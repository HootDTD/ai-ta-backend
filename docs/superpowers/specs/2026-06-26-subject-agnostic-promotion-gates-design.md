# Subject-Agnostic Promotion Gates — Design Exploration

- **Date:** 2026-06-26
- **Branch:** `ApolloRun` (feature branch; never merge to `main` without explicit go-ahead)
- **Status:** Brainstorm output — shared problem framing + chosen design direction.
  **NOT an implementation plan.** Next step (when ready) is `writing-plans`.
- **Relates to / evolves:** `2026-06-23-subject-fluid-apollo-design.md` (the hand-authored
  subject-profile approach this supersedes), `apollo/provisioning/promotion_lint.py`,
  `apollo/provisioning/subject_profile.py`, `apollo/provisioning/promote.py`,
  `apollo/provisioning/pairing_gate.py`.
- **Owner doc (drift contract):** `docs/architecture/apollo.md`.

---

## 1. Context & evidence

Apollo auto-provisions teachable problems from course materials. A scraped problem + its
constructed reference solution must pass an 8-gate "promotion lint"
(`promotion_lint.py`) before it becomes teachable (tier 1 → tier 2 + projected `:Canon`).

The current "subject-fluid" attempt (PR #64, `subject_profile.py`) is hand-authored discrete
**profiles** — `quantitative_symbolic` (all 8 gates) and `qualitative_argumentative`
(gates 1/2/3/8 + an LLM faithfulness check) — chosen per subject by a heuristic probe
(`detect_profile`, mostly "has numeric givens → symbolic").

A live E2E on a real Purdue AAE 333 (aerodynamics) homework (staging, ss=4, doc=6, run=2)
ran end-to-end then **promoted 0/6**:
- 5 rejected at **gate 5**: targets are prose ("boundary layer thickness", "wall shear
  stress", "u velocity at y=δ/2"), not a single symbol the terminal equation computes.
- 1 rejected at **gate 4**: a freshly auto-minted concept has no `canonical_symbols`
  seeded, so any real symbol is "foreign".

Aerodynamics was auto-detected `quantitative_symbolic` (it has numeric givens), so the
symbolic gates ran and blocked it — even though it's "quantitative numbers but prose
targets", a shape neither built-in profile handles.

## 2. Problem framing

The 8 gates conflate **three different kinds of "validity"**:

- **A. Structural integrity** (gates 1, 2, 3, 8) — is this a well-formed, closed, acyclic,
  non-duplicate typed graph? About the graph *object*, not the reasoning. Epistemology-independent.
- **B. Soundness of the reasoning** (gates 4, 5, 6, 7) — does the solution validly *derive its
  answer*? Today this is exactly **one** soundness theory — "a closed system of symbolic
  equations computes a single free-symbol target" — hardwired in as if universal.
- **C. Faithfulness / grounding** (`pairing_gate`, the LLM judge) — is the solution faithful to
  the authored source? Already separate, already LLM-based, already runs regardless of profile.

The bug isn't "gates 4–7 are wrong". It's that **gates 4–7 are one instance of (B), and the
system has no vocabulary for any other instance.** A subject profile toggles that instance on/off
*wholesale*: "off" collapses (B) to only the softer LLM check; "on" force-applies the symbolic
theory even when the problem isn't symbolic.

Two things the evidence forces:

1. **A registry doesn't escape the trap.** A set of validators keyed per-subject *or* per-
   epistemic-shape is still a **closed enumeration**. Different textbooks/teachers use different
   variable mappings (`P` vs `p` vs `π`; `δ` vs `θ`); any finite set of validators (and any
   per-concept `canonical_symbols`/`normalization_map` table) bets you anticipated the next
   textbook's conventions. You're wrong on the (N+1)th and back to hand-authoring.
2. **The hardwired gates reach *outside* the problem.** Gate 4 checks symbols against the
   *concept's* `canonical_symbols` table (an external, per-textbook artifact — also why a fresh
   concept fails). Gate 5 assumes the target is a free symbol *in an equation*. Both are external
   assumptions; both are what make the system subject-specific.

## 3. The agreed principle

> **Validity is checked against the problem's OWN authored content and its internal
> consistency — never against any external, course/subject/textbook-level convention.**

There is **one pipeline**: no subject detection, no profile, no per-epistemology validator
registry, no fixed list of "answer kinds". Checks are either universal-structural or
**self-activate on content features**.

The **correctness oracle shifts**: from "independently verify the solution is right" to
**"verify the graph faithfully encodes the professor's authored solution."** For math you then
have *two* oracles (author + a mechanical SymPy check); for everything else, *one* (the author).
This is the load-bearing move that makes "subject-agnostic" compatible with "never ship a
false-GREEN": residual false-GREEN risk on non-math is contained operationally (see §6), not
pretended away.

## 4. The three-tier model

1. **Universal, deterministic, always-on:** structural integrity (today's 1/2/3/8) + a
   *graph-shaped* target-reachability check + dedup. No subject assumptions, no external tables.
2. **Self-activating deterministic rigor (fires on content features):** when a step genuinely
   *is* a parseable equation, the SymPy checks (well-formedness, symbolic system-closure, symbol
   grounding) run on it. Math hands you a cheap mechanical oracle; nothing else does. This tier is
   **additive** — its *absence never blocks a subject*; that problem simply rides tiers 1 + 3.
3. **Faithfulness (the semantic oracle):** the existing two-phase judge, grounded on the authored
   solution. Carries "is the reasoning actually right" for everything math can't mechanically check.

### 4.1 Gate 5 becomes a graph property (no answer-kind enum)

Today gate 5 = "terminal equation's free-symbols include the symbol target" — subject-specific
three ways over. Generalize to a **pure graph property**: *there is a unique terminal sink, and
the node carrying the problem's answer is what the chain terminates in.* Target-**kind**-agnostic.
Faithfulness confirms the semantics ("the terminal step actually produces what was asked"); the
SymPy layer, *if it fires*, adds "and the terminal equation computes it symbolically".

**No typed-target enum.** Tagging targets with a fixed list (`symbol`/`quantity`/`claim`) just
moves the closed enumeration from "subject" to "answer-kind" — a novel target (a proof, a
construction, a classification) needs a new entry + new code, and the (N+1)th problem bites again.
The *only* thing we ever enumerate is the set of **bonus mechanical oracles we happen to have**
(today: SymPy; later maybe units-checking or code-execution) — safe, because a missing oracle
never blocks anything. (Descriptive typed-target metadata for the future practice-generator —
Goal 2 — is **deferred (YAGNI)**; add it when Goal 2 actually needs it.)

### 4.2 Symbol grounding becomes internal

"Foreign symbol" stops meaning "not in the course's canonical table" and starts meaning
**"a term the solution uses that the problem statement never gives and the solution never
defines."** Pure internal closure — each problem carries its own symbol grounding; no table to
author, no textbook convention to track. This also fixes the gate-4 bootstrapping failure for free
(fresh concept needs no seeded table).

## 5. Two shared safety mechanisms (every direction relies on these)

**Back-compat anchor (the 41 seeded ss=2 `:Canon` stay byte-identical).** The mechanical (SymPy)
checks still **hard-reject** whenever a problem contains real equations. For a concept that already
has a seeded symbol table — the 41's exact situation — the symbol check **keeps using that table**,
so its verdicts don't move. The internal-grounding path (§4.2) kicks in **only when no table
exists** (new content). Net: the 41 see the same checks as today. **Proven by a characterization
test** — run the new pipeline over all 41, assert identical promote/reject and identical failure
reasons *before* any refactor.

**Honesty handle (contain the softer non-math bar).** Every promoted problem is stamped with *how
it cleared*: **"mechanically verified"** (a mechanical oracle fired and passed) vs
**"faithfulness-only"** (no mechanical oracle applied — rode structure + the LLM judge). That stamp
is the operational lever: faithfulness-only problems can be held to a higher human-review bar or
batch-audited before student exposure, on top of the existing `APOLLO_AUTOPROVISION_ENABLED`-off
default and the 3B2h quarantine. Distinct from `solution_source` ("authored/generated" = *where the
solution came from*); this is *how hard we checked it*.

## 6. Candidate directions

All directions share §3–§5 and differ only in how much the code is restructured to reflect it.

### Direction A — Minimal surgery: make the existing gates self-activating
Keep the single `run_promotion_lint` and the 8 gates. Change gates 4/5/6/7 from "always run (or
profile-toggled)" to "run only when their content precondition holds" (equations present → run;
none → pass vacuously). Gate 5 gets the §4.1 graph-property rewrite. Delete `detect_profile` + the
two profiles; the `active_gates` seam stays but is driven by content, not a stored profile.
- **Pro:** smallest diff; symbolic path for the 41 essentially untouched → back-compat trivial to
  prove; fastest route to aerodynamics promoting.
- **Con:** the three-tier idea stays *implicit* (scattered `if equations…` preconditions inside one
  function); the next mechanical oracle means surgery on the monolith; the "missing oracle never
  blocks a subject" safety property is true only by careful coding, not by structure.

### Direction B — Explicit tiers: structural core + additive rigor layers + faithfulness  ✅ CHOSEN
Refactor the lint into three named things:
- a **structural core** (1/2/3/8 + the universal target-reachability check) — always runs, pure;
- a list of **rigor layers**, each a pair `(applies?(graph), check(graph))`. v1 ships exactly one —
  the symbolic layer (today's 4/6/7 + the symbolic half of 5), whose `applies?` is "there are
  parseable equations";
- the **faithfulness stage** (existing judge), now explicitly named as the semantic oracle.

Promotion = structural core passes **and** every *applicable* rigor layer passes **and**
faithfulness passes.
- **Pro:** the model is legible *in code* (floor / additive oracles / oracle-of-last-resort). The
  safety property is **structurally enforced** — a rigor layer can only add a rejection to content
  it *applies* to; it physically cannot block content it doesn't apply to. The next subject is
  genuinely free; the next mechanical oracle is a new layer with no core edits.
- **Con:** a real refactor of the module the codebase calls the "auto-provisioning SAFETY CORE" —
  high blast radius; needs characterization tests locking current behavior *first*. With one rigor
  layer today the plugin seam is mildly speculative — but unlike the answer-kind list, it encodes
  the one principle we care about ("oracles are additive, never blocking").

### Direction C — Rigor as confidence, not a gate  ❌ rejected (marks the boundary)
Let SymPy raise/lower a confidence score instead of rejecting; hard-gate only on structure +
faithfulness; mechanical failures quarantine rather than block.
- **Why not:** breaks the invariant head-on — a bad *symbolic* solution could promote on
  faithfulness alone (false-GREEN; the 41 move). To keep the invariant you'd say "SymPy stays a
  hard gate for problems that present as symbolic" — which *is* Direction B. Named only to mark the
  boundary: **mechanical rigor must stay a hard gate wherever it fires.**

## 7. Decision

**Anchor on Direction B as the target architecture** — it's the only option that makes "the next
subject is free" a *structural guarantee* rather than a property kept manually true. Safe sequencing
(a planning detail, not part of this brainstorm): land **A's behavior first** (characterization-test
the 41 unchanged, get aerodynamics promoting), then do **B's refactor** as a separate,
behavior-preserving step.

## 8. Hard constraints preserved

- **No fluid-mechanics regression:** the 41 seeded ss=2 `:Canon` promote exactly as now (§5 anchor).
- **Asymmetric safety:** a false-GREEN must never ship; a false-RED only quarantines a good problem.
- **Citations + semantic-scope enforcement** remain non-negotiable product invariants (untouched).
- **Gates stay pure / DB-free / LLM-free;** the LLM lives at construction + the one faithfulness
  stage. Profile/active-gate selection is replaced by content-derived applicability computed by the
  caller.
- **Drift contract:** update `docs/architecture/apollo.md` + `last_verified` in the same commit as
  any code change.

## 9. Open questions for the planning stage (not decided here)

- **What precisely counts as "internal grounding"** for new (table-less) content — givens +
  `definition`/`variable_mapping` steps only, or also a per-problem author-declared symbol set?
  Must be a *superset-accept* relative to the seeded-table behavior so the 41 never move.
- **Characterization-test corpus:** snapshot the 41's current promote/reject + failed_gate as the
  regression oracle before any code moves.
- **`pairing_gate` sufficiency for non-math:** does the faithfulness judge need to explicitly assert
  "terminal step produces the target", or does the §4.1 graph property + existing claim-decomposition
  cover it?
- **Where the "how it cleared" stamp lives** (payload field vs column) and how it gates student
  exposure.
- **#64 harvest:** keep schema optionality (`target_unknown`/`given_values` optional) and the
  `active_gates` seam; drop `detect_profile`, the two profiles, and the `apollo_subjects`
  profile columns/migration.
