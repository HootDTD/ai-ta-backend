# Apollo Grader — Clarification-Loop Design Direction (G2) — Handoff

**Date:** 2026-06-29
**Status:** Mid-brainstorm. **No code written this session for this design** — this is a
direction-setting + investigation handoff. Resume the brainstorm at **Q2** (see §5).
**Branch state:** `fix/apollo-grader-abstention-mvp` (the MVP abstention fix from the prior
session; unrelated to this design except as the work that *exposed* the problem). No new
commits this session beyond this doc.

---

## 0. TL;DR

This session did two things:

1. **Established the concrete basis** for what the high `unresolved_rate` (the abstention
   driver) actually *is*. Verdict: it is a genuine **resolver-recall** property (the thing
   we've been calling **G2**), **not** an artifact of single-pass testing. A *passive* deeper
   conversation would not fix it. (§2)

2. **Pivoted the fix direction.** User rejected the planned **Phase 1b** (teacher-authored
   exact-match reference aliases) as too static / too much teacher burden / brittle. New
   direction: **semantic match as a *clarification trigger*, not auto grading credit** —
   Apollo asks a targeted follow-up when a student's explanation is plausibly-but-ambiguously
   correct, and the student's clarification decides the outcome. We then established that this
   clarification loop **is itself the solution to G2**, reframed. (§3–§4)

The design is **not yet specified**. We reached a confirmed architectural choice (live,
mid-conversation — "Option A") and a core 3-band model, then paused for this handoff with Q2
and several sub-questions still open (§5).

---

## 1. Lineage — read these, in this order

| Doc | Why it matters here |
|---|---|
| `docs/_archive/handoffs/2026-06-26-apollo-grader-node-recovery-handoff.md` | The 4-phase node-recovery work + the **SHADOW grader** concept (graph-sim grader is gated OFF; live grade still = LLM matcher). Origin of Phase 1b. |
| `docs/_archive/handoffs/2026-06-26-apollo-grader-mvp-fix-handoff.md` | The abstention **MVP fix** (type-aware nc floor + teachable-concept filter). §4 "why Phase 1c does what it does"; §6 lists **G2** as deferred. This is the immediate predecessor. |
| `docs/_archive/experiments/2026-06-26-apollo-grader-node-recovery-e2e/FEEDBACK.md` | Full E2E report: live score matrices (15 econ + 2 fluids), gaps **G1–G7**, the abstention arithmetic. G2 = resolver recall. |
| `docs/_archive/plans/2026-06-23-apollo-grader-phase1b-plan.md` | **The plan being replaced.** This is the static teacher-authored `exact_aliases` approach the user objected to. Read it to know exactly what we're *not* doing. |
| `docs/architecture/apollo.md` | Drift-contract owner doc for the grader. Must be reconciled when this design lands. |
| Memory: `apollo-grader-abstention-defect.md` | The abstention defect + MVP fix history, incl. "remaining abstentions all `unresolved_rate`, ZERO `normalization_confidence` → tracked as G2." |
| Memory: `apollo-misconception-bank-decisions.md` | Relevant to the **"incorrect clarification → misconception evidence"** outcome below. User wants a **trust-gradient / promotion pipeline** for misconceptions, NOT binary approved-only. ⚠️ The memory references design docs "in ai-ta-backend/docs/" but they are **not committed** (a `find` this session returned none) — locate or re-derive before wiring misconception capture. |

---

## 2. Investigation findings — the concrete basis (read before designing)

A read-only subagent traced the probe + the production chat/resolution paths. Verbatim
conclusions (all citations are `ai-ta-backend/...`):

### 2.1 The E2E probe was multi-turn, not single-pass — and *generous*
- `scripts/apollo_grade_probe.py` runs a real multi-turn session: it POSTs each student
  message as its own `/apollo/sessions/{id}/chat` turn (line ~401), then `/done` (line ~408).
  Strong scenarios = 3–5 turns; weak = 1–2.
- BUT the turns are **pre-scripted and non-reactive** — Apollo's reply is captured and
  *never consumed* to choose the next message (the loop iterates a fixed `msgs` list).
- The scenarios (`scripts/_macro_scenarios.py`, `_BERNOULLI_SCENARIOS`) **deliberately state
  every reference node** in the strong/partial variants. So the probe fed the resolver dense,
  on-topic content — if anything an *easy* test, not a stingy one.
- The student graph is built **incrementally per turn** server-side
  (`apollo/handlers/chat.py` → `parse_utterance` → `store.write_nodes`), exactly like prod.
  Grading at `/done` resolves over the *accumulated* graph (`done_grading.py:228`).

### 2.2 Production Apollo is reference-blind; resolution is Done-time only
- Apollo's turn = `draft_reply` (`apollo/agent/apollo_llm.py:93–160`). Inputs: `history`,
  `kg_summary`, `problem_text`, `history_summary`. **No coverage, no confidence, no reference
  graph.** `kg_summary` summarizes only the *student's own* taught nodes
  (`store.summarize_for_apollo`). Apollo's "confusion" is a generic LLM persona.
- `resolve_attempt` / coverage run **only in the grading path** (`done_grading.py:228`,
  `graph_compare/core.py:101`), reachable only from `/done` + the retry janitor. **Nothing
  resolves against the reference during chat.** The live UI has no mid-chat unresolved signal.
- ⇒ The probing behavior the user intuited ("Apollo asks about low-confidence topics")
  **does not exist today** — on *both* ends (reference-blind turns + no live resolution).

### 2.3 `unresolved_rate` is a MECH-B signal, by construction
`unresolved_rate` (`apollo/grading/abstention.py:69–77`, gate `> 0.35`) counts **student**
nodes whose `resolution != "resolved"`. Therefore:

- **MECH-A** (student never addressed a topic) → produces **no student node** → contributes
  **nothing** to `unresolved_rate`. It shows up on the *reference* side as a coverage
  `missing_key` (`graph_compare/coverage.py:42–48`), which does **not** trigger abstention.
- **MECH-B** (student articulated it, resolver couldn't map it) → produces an `unresolved`
  student node → **this is what drives the abstention gate.** The resolver's tiers are
  exact → symbolic → derived → alias → fuzzy(≥0.9) → **one silent LLM adjudication** → else
  unresolved (`resolution/resolver.py`, caps in `candidates.py`).
- **MECH-C** (numeric-only / value not expected in prose) → unresolvable by design.

**Key consequence:** more *passive* conversation does NOT lower `unresolved_rate` — it feeds
more nodes through the same failing resolver. The abstention problem is a true **resolver-recall
(G2)** problem, not a single-pass artifact. What passive conversation *does* improve is
*coverage* (the MECH-A axis), which is a different metric that gates nothing.

---

## 3. The feedback that triggered the pivot (verbatim intent)

> Phase 1b's exact-only, hand-authored reference aliases are too static and create unnecessary
> teacher burden. Apollo should not require teachers to prewrite many exact acceptable answers
> for every question. This also limits dynamism and weakens Apollo's value as a conversational
> tutor.
>
> **Preferred direction:** Use fuzzy/semantic matching for possibly-correct explanation nodes
> as a **clarification trigger, not automatic grading credit.** If a student gives an ambiguous
> but plausibly correct explanation, Apollo asks a targeted follow-up. If the student clarifies
> correctly → the node is confirmed and resolved. If the student clarifies incorrectly → Apollo
> gains explicit misconception evidence. If the student stays vague → the node stays unresolved
> / low-confidence.

---

## 4. The design direction reached (confirmed in brainstorm)

### 4.1 Architecture choice — **Option A: live, mid-conversation** (CONFIRMED by user)
Detection runs *during teaching*; Apollo works the targeted follow-up into its next confused
reply. (Rejected: B = a separate clarification round at Done; C = hybrid live + Done cleanup.
But see §4.4 — a Done-time cleanup may still be needed as a catch-net.)

### 4.2 The reframe that makes this a swap, not a bolt-on
Today the resolver's *last resort* for an ambiguous node is **a single silent LLM adjudication**
that quietly guesses "does this mean reference-node X?" and, when unsure, drops to `unresolved`.
That silent guess is exactly the low-recall step driving abstention. **This design replaces the
silent machine guess with Apollo asking the student.** The clarification loop occupies *exactly
the band where the resolver currently shrugs.*

### 4.3 The core model — a per-node **3-band router** (live)
- **Confident match** (≈ exact/symbolic/fuzzy≥0.9) → **resolve silently, no question**, credit
  as today. *(Q2 — see §5 — asks whether even these should require a quick confirm.)*
- **Ambiguous band** (plausible, not confident — where the silent LLM guess lives today) →
  **do NOT credit; trigger a targeted Apollo follow-up.** Outcome decides:
  - clarifies **correctly** → confirmed / resolved (likely a new "clarification-confirmed" tier),
  - clarifies **incorrectly** → **misconception evidence** (ties to G4 / misconception bank),
  - stays **vague** → unresolved / low-confidence.
- **Low / no match** → leave it (genuine coverage gap or clearly off-topic; not probed here).

### 4.4 This IS the G2 solution, reframed (the strategic finding)
- The clarification loop converts **ambiguous-band unresolved** nodes → resolved/refuted using
  the **student's clarification as the resolution signal** instead of the silent LLM guess.
  Those are the precise nodes inflating `unresolved_rate`. So it lowers it *at the source*.
- **The job-swap (the real leverage):** the resolver no longer has to **decide correctly**
  (hard, high-precision); it only has to **notice plausibility** (easier, high-**recall**, can
  be sloppy on precision because the *student* filters false positives). A high-recall /
  low-precision **detector** is far more tractable than a high-recall / high-precision resolver.
- **Boundary (be honest about this):** effectiveness is bounded by the **detector's** recall,
  not the resolver's final-decision recall. MECH-B splits:
  - **B1** — lands in the ambiguous band → Apollo asks → resolved/refuted ✓ caught.
  - **B2** — similarity so low the detector never flags it → no probe → still unresolved ✗ missed.
- **Residual work G2 still implies:**
  1. a **decent-enough detector** (keeps B2 small *and* keeps Apollo from asking 10 follow-ups
     a session — a smarter offline resolver = fewer needed clarifications), and
  2. a **MECH-A / B2 catch-net** — a Done-time cleanup pass (the hybrid-C idea) that surfaces
     still-uncovered *important reference* nodes and lets Apollo probe them before grading.
- **Bonus that's easy to undervalue:** after this loop, **"unresolved" actually means
  something** — "the student was given a direct chance to clarify and couldn't" — instead of
  "a hidden LLM coin-flip missed." That makes the abstention gate **valid**, not merely
  numerically lower. (Earning the number vs gaming the number look identical on a dashboard;
  this earns it.)

### 4.5 Correction to an earlier claim (keep the reasoning straight)
Earlier this session I said "a real Apollo conversation would NOT lower `unresolved_rate`."
That holds for **passive** extra conversation (more teaching → same silent resolver). It does
**not** hold for this design, which is **active, targeted disambiguation** — it injects a human
decision exactly where the resolver is uncertain. Both statements are true; the difference is
*targeted vs passive*. Don't let a future reader cite the earlier line to dismiss this design.

---

## 5. OPEN — resume the brainstorm here

**Q2 (pending, asked but unanswered):** Does a **confident** semantic match resolve silently
(my proposed model), or should *even confident matches* require a quick student commit before
earning credit (a stricter "never trust fuzzy, always make the student commit" stance)? The
user paused to ask the G2 question before answering this.

Then the remaining sub-questions (not yet opened), roughly in priority order:

1. **Cheap detector mechanism + band boundaries.** Lexical fuzzy (cheap, exists) vs embedding
   similarity vs incremental resolver tiers. What runs *per turn* without paying a full resolve?
   Where exactly are the confident / ambiguous / low boundaries? (Note: reference-node embeddings
   could be precomputed.) The detector wants **high recall, tolerant precision**.
2. **Pedagogical integrity of the follow-up (critical, unexplored).** Apollo's clarifying
   question must **not leak the target** — if the question reveals the correct answer, a
   "correct clarification" proves nothing and the signal is worthless. How much of the reference
   do we expose to the turn generator, and how do we constrain the prompt to *make the student
   commit* rather than *reveal*?
3. **Re-evaluation after clarification.** What re-scores the node? Likely a focused LLM
   adjudication over (original utterance + clarification). At what confidence cap does a
   clarified-correct node resolve? ⚠️ **Interaction with Phase 1c:** the
   `normalization_confidence` floor is **0.85**. A "clarification-confirmed" tier must sit **≥
   0.85** or clarified-correct nodes would still trip the nc abstention gate. This is a hard
   constraint, not a preference.
4. **Loop / budget control.** Max follow-ups per node (1? 2?); max ambiguous nodes probed per
   session before it feels like an interrogation; **prioritize by node importance** (don't burn
   the budget on a peripheral node).
5. **Per-attempt clarification state.** New state machine per node: `never-asked` /
   `asked-pending` / `confirmed` / `refuted` / `vague`, so Apollo doesn't re-ask and `/done`
   knows each node's final disposition. Where does this live (KG store? attempt state?).
6. **Misconception capture scope.** Emit structured misconception *evidence* only, vs full
   integration with the misconception bank's trust-gradient/promotion pipeline (G4). Likely
   emit-evidence now, integrate later — but confirm. (See memory `apollo-misconception-bank-decisions`.)
7. **Architecture seam.** Live detection ⇒ resolution (or a cheap proxy) must run incrementally
   AND feed Apollo's turn generator, which is currently reference-blind + Done-time-only. This is
   the biggest code change: a per-turn detect→flag→steer path that doesn't exist yet.
8. **Done-time cleanup pass (hybrid-C).** Decide whether to add it as the MECH-A/B2 catch-net.

**Terminal goal of the brainstorm:** a design spec. Per workspace convention (CLAUDE.md:
per-repo planning sessions write to the owning repo's `docs/_archive/`), the spec goes in
**`ai-ta-backend/docs/_archive/specs/YYYY-MM-DD-apollo-clarification-loop-design.md`** — NOT the
workspace `docs/superpowers/specs/` and NOT a new `docs/superpowers/` tree inside the sub-repo.
Then an implementation plan in `docs/_archive/plans/`.

---

## 6. Code & git state

- **Branch:** `fix/apollo-grader-abstention-mvp` — commits `79c6412` (type-aware abstention
  floor + teachable-concept filter) and `1f79f19` (E2E findings + MVP handoff). This is the
  prior session's MVP work; **PR not opened** (GITHUB_TOKEN was stale — see memory
  `github-access-wiring`). It is the work that *exposed* G2, not part of this design.
- **This session wrote no code.** Brainstorm + investigation + this doc only.
- The graph-sim grader remains a **SHADOW** chain (gated OFF; live grade = LLM matcher). This
  design changes the **live conversation** (Apollo's turns + the student graph), which both the
  live LLM grader and the shadow grader consume — so it improves grading regardless of promotion.
- Throwaway harness files may be uncommitted in `scripts/` (probe timeout bump, etc.) — ignore.

## 7. First moves next session
1. Re-read §2 (the concrete basis) and §4 (the direction) — they're the load-bearing context.
2. Re-invoke the brainstorming skill and answer **Q2**, then walk §5 questions 1→8.
3. Especially nail **§5.2 (don't-leak-the-answer)** and **§5.3 (clarification tier ≥ 0.85)** —
   those two are where the design most easily goes wrong.
4. Only after the design is approved: write the spec to `docs/_archive/specs/`, then a plan.
