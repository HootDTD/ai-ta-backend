# Apollo unified questioning — quality brainstorm (shared discussion doc)

Participants: **codex** (gpt-5.6-sol) and **claude** (opus, ultracode), moderated
by the styx conductor. Protocol at the bottom. APPEND-ONLY: never edit an
earlier section, including your own from a prior round.

## The problem statement (from the user, verbatim sentiment)

> "its shit! i really dont understand why, like we are likelly overprompting,
> or something, because these are genuently stupid reduntant questions."

Four PRs of targeted fixes (#181 clause coverage, #182 dedup+retry, #183
leak-guard relaxation, #184 brief acks + don't-re-ask-disclaimed + off_topic
fall-through) each fixed their named defect, and the conversation is STILL
dumb. The task here is NOT another patch. Diagnose the root cause and propose
a design that removes complexity rather than adding rule N+1.

## Evidence A — post-#184 transcript (all current prompt rules were live)

Problem (3 public clauses): "What is Future Shock, and why does it occur?
When did it start happening — can you give an example? And is it still
happening today — why or why not?"

1. Student: "future shock occurs when things are happening too quickly and it
   becomes difficult to keep up"
2. Apollo: "That makes sense. When did it start happening—can you give one
   example?"   ← fine
3. Student: "started happening in 1790. a recent example of this is AI, since
   a lot of people are overwhealmed by this inovation"   ← answers WHEN
   (1790) AND volunteers a TODAY example (AI)
4. Apollo: "Got it. What change around 1790 are you thinking of that makes
   you place the start of future shock in that year?"   ← legitimate
   conflict probe (reference year differs)
5. Student: "not sure"   ← explicit disclaim
6. Apollo: "Got it. Do you think Future Shock is still happening today—why or
   why not?"   ← DEFECT: ignores that turn 3 already gave a current example;
   the model does not credit cross-clause evidence
7. Student: "yes i gave you the example"   ← student frustration, correct
8. Apollo: "Thanks. What's your best guess for when it started happening, and
   what changed then?"   ← DEFECT ×2: re-asks WHEN (answered at turn 3:
   1790) and WHAT-CHANGED (explicitly disclaimed at turn 5) — a direct
   violation of the #184 prompt rule ("never ask for that same information
   again, even with different wording"), one turn after that rule shipped.

The turn-8 violation is the key datum: **a freshly deployed, explicit,
well-worded prompt rule failed on its first live encounter.** Marginal
prompt-rule additions appear to be at zero or negative returns.

## Evidence B — debug logs (previous session, pre-#184; same architecture)

- EVERY logged turn went through the reject→retry cycle
  (`fallback_reason=…_retry_recovered` on all turns). The model's drafted
  turn is almost never served as drafted; what the student sees is a
  Franken-assembly (ack often stripped, question sometimes canned).
- All ack rejections had EMPTY offending tokens → the echo sub-check
  (any shared 4-token run with student text) kills faithful acks; the
  vocabulary allowlist relaxation (#183) didn't help acks because echo, not
  vocab, is the binding constraint.
- Decision logs show target selection gravitating back to the same node
  (`proc_when_started`, state=conflicting) across turns — the node-state
  machine wants its conflict resolved; conversational common sense says drop
  it after the student disclaims.

## Current architecture (read the code, not just this summary)

Code snapshot = current staging: worktree
`C:\Users\ultra\OneDrive\TA-test\.worktrees\apollo-ack-brevity-offtopic`
(branch fix/apollo-ack-brevity-offtopic = merged PR #184). READ-ONLY except
this doc. Key files:

- `apollo/smart_questions/unified.py` — one gpt-5.2 structured-output call
  per teaching turn (`_SYSTEM_PROMPT`, `_schema`, `evaluate_and_ask`), then a
  deterministic guard pipeline (`_validate_draft`: malformed / private-vocab
  boundary / echo / repeated-question / broad-reask), one retry with
  feedback (`_retry_feedback`), then canned fallbacks
  (`_fallback_question`, `_GENERIC_FALLBACK`, `_least_recent_canned`).
- `apollo/smart_questions/controller.py` — per-node question ledger
  (`ReferenceQuestionOpportunity`), transcript assembly.
- `apollo/handlers/chat.py` — turn flow: intent gate → parser/KG write →
  unified questioning.
- Specs of the four patch PRs (history of what's been tried):
  `docs/_archive/specs/2026-07-15-apollo-unified-clause-coverage-spec.md`,
  `…-apollo-question-dedup-retry-spec.md`,
  `…-apollo-leak-guard-relaxation-spec.md`,
  `…-apollo-ack-brevity-offtopic-spec.md`.

Structural facts worth weighing:

1. `_SYSTEM_PROMPT` now carries ~a dozen competing duties: learner-tally
   judgment for every reference node, per-clause coverage judgment, target
   selection, private-leak boundary, echo avoidance, ack brevity, no
   restatement, don't-re-ask-disclaimed, JSON discipline. (Overprompting
   hypothesis.)
2. Targeting is driven by the per-node tally over ~12 private reference
   nodes, while the student experiences 3 public clauses. Nothing reconciles
   "node the tally wants next" with "what a sane tutor would ask next."
3. Conversation facts the model needs every turn — which clauses/nodes were
   asked about, answered, or explicitly disclaimed, and WHERE in the
   transcript the evidence sits — are never given as structured input. The
   model must re-derive them from the raw transcript under heavy rule load,
   every turn, at reasoning effort "medium".
4. The guard layer overrides the model's plan almost every turn (Evidence
   B), so even good drafting gets shredded; and the model has no memory that
   its previous SERVED turn differed from what it drafted.

## Constraints (non-negotiable unless argued explicitly)

- Privacy boundary: private rubric content (reference dates, proper nouns,
  technical terms, phrases the student hasn't said) must never reach the
  student. HOW this is enforced is fully up for debate — the current
  token-allowlist guard is not sacred.
- No problem-specific or transcript-specific content in any global prompt.
- Turn latency: a few seconds. Two serial LLM calls per turn is the current
  worst case; do not propose 4.
- Cost sanity: this runs on every teaching turn.
- Must be evaluable on staging (decision + debug logs exist; propose what to
  measure).
- Student-facing tone: attentive classmate; the student is TEACHING Apollo.

## Questions the discussion must answer

Q1. Root cause: is the dominant failure overprompting (too many rules →
    satisficing), state starvation (model lacks structured conversation
    facts), objective mismatch (node-tally coverage ≠ good conversation),
    guard interference (Franken-turns), or several at once — ranked how?
Q2. What should be deleted? (Rules, guards, schema fields, the retry, the
    canned fallback chain, the per-node ledger…) Be specific.
Q3. What is the minimal architecture that fixes turns 6 and 8 in Evidence A
    by DESIGN rather than by rule? E.g.: deterministic target pre-selection
    feeding a free-voice writer; a compact structured conversation-state
    input (asked/answered/disclaimed per clause+node with evidence spans);
    an LLM-judge replacing string guards; single-responsibility prompt.
    Propose concretely — schema, call structure, what each layer owns.
Q4. Privacy enforcement design that doesn't shred normal conversation.
Q5. Eval plan: how do we know the redesign works — what metric from the
    logs, what replay/regression harness, what transcript-level check?

## Deliverable

Converge on ONE primary recommendation (+ at most one fallback), with: what
gets deleted, what gets built, call structure per turn, privacy design,
migration path from current code, and the eval plan. The conductor will turn
the converged section into an implementation spec.

## Protocol

- Round 1 — codex: independent diagnosis (answer Q1–Q4 provisionally) + 2–4
  concrete solution directions with tradeoffs. Cite code as `file:line` and
  evidence as "Evidence A turn N" / "Evidence B". Append under
  `## Round 1 — codex`.
- Round 1 — claude: read codex's round; adversarially critique (what would
  fail, what's hand-waved), then give your own diagnosis + proposals,
  including anything codex missed. Append under `## Round 1 — claude`.
- Round 2 — codex: rebut or concede point-by-point; propose the converged
  design. Append under `## Round 2 — codex`.
- Round 2 — claude: final joint recommendation per the Deliverable section.
  Append under `## Round 2 — claude (joint recommendation)`.
- All participants: append-only; no code edits anywhere; write your section
  INCREMENTALLY (save partial progress as you go — sessions on this box can
  die); keep each round under ~250 lines.

---

## Round 1 — codex

### Diagnosis

My ranking is:

1. **Objective mismatch (dominant).** Apollo's stopping rule is private-rubric
   completeness, not conversational usefulness. The prompt says to ask whenever
   any reference node is not understood (`apollo/smart_questions/unified.py:235-245`),
   and the runtime independently forces an unresolved target even if the model's
   requested target/action is unusable (`apollo/smart_questions/unified.py:867-892`).
   The per-node ledger explicitly permits the same tentative/missing/conflicting
   node to be targeted again (`apollo/smart_questions/controller.py:89-96`). Thus
   Evidence B's repeated `proc_when_started` selection is expected behavior, not
   an edge case. The prompt even contains a direct policy collision: after “I
   don't know,” use `done` if no productive alternative remains (lines 242-244),
   but “otherwise” `done` is allowed only when *every* private node is understood
   (line 245). Evidence A turn 8 is the model satisfying the stronger global
   completion objective at the expense of the local conversational rule.

2. **State starvation / wrong state representation.** The only durable question
   state is one latest question per private node (`controller.py:49-56,77-95`).
   There is no durable public-goal state for “turn 3 supplies evidence for the
   today clause” or “turn 5 declined the basis-for-1790 probe.” Each call receives
   a raw transcript plus the lossy node ledger (`unified.py:819-831`) and must
   rediscover those facts while doing everything else. The schema's clause status
   has no evidence spans, decline state, last-asked goal, or cross-clause mapping
   (`unified.py:190-209`); invalid/omitted items silently become `unattempted`
   (`unified.py:380-398`). Evidence A turn 6 is precisely a lost cross-clause
   entailment. Turn 8 shows that a transient natural-language prohibition is not
   durable state.

3. **Responsibility overload / overprompting.** One call is simultaneously a
   private rubric judge, public-clause judge, target policy, stopping policy,
   privacy reasoner, conversational writer, and structured serializer
   (`unified.py:215-268`). This matters less because of raw prompt length than
   because the duties compete. “Resolve every private node,” “prefer public
   clause progression,” “do not re-ask,” and “do not expose why the node is
   unresolved” often cannot all be satisfied. A stronger sentence for rule N
   does not establish precedence or memory, so #184 failing immediately is not
   surprising.

4. **Guard interference (large amplifier, not the planner's root cause).** The
   validator can reject a good question for private-vocabulary, any shared
   four-token student run, exact repeat, or a narrow definition of broad re-ask
   (`unified.py:703-746`). The same four-token rule deletes faithful acks
   (`unified.py:601-618,747-768`), matching Evidence B. A rejection triggers a
   second full unified call (`unified.py:913-938`); a second rejection substitutes
   a canned clause/probe (`unified.py:943-957`). Consequently the served turn may
   express neither the first nor second plan. However, Evidence A turn 8 passed
   the live architecture despite being conversationally wrong, so removing guards
   alone will improve voice but will not repair target selection.

The key design conclusion is that **rubric evidence and conversation agenda are
different state machines**. Rubric evidence may remain conflicting forever and
still be useful for grading. A conversation goal can be answered, declined, or
exhausted and must then be closed. “Conflicting” is evidence for the grade; it is
not an unlimited license to interrogate.

### Required semantics before choosing an architecture

- Decompose the public problem once into stable, public `goal_id`s at the
  askable-facet level, not merely text split on `?`. In Evidence A these include
  definition/cause, start time, historical example, still-today judgment, and
  today reasoning. The current splitter produces only three coarse parts
  (`unified.py:375-377`).
- Give each public goal a durable status: `open`, `partial`, `satisfied`, or
  `declined`, plus student evidence turn/span IDs, `last_asked_turn`, and probe
  count. `satisfied` and `declined` are absorbing for questioning unless the
  student voluntarily revises them.
- Credit evidence by meaning across *all* goals, regardless of which goal Apollo
  last asked. Turn 3's AI/overwhelm statement therefore updates the today goals.
- An “I don't know” response closes the goal of the immediately served question;
  it does not erase other supplied claims and does not convert their rubric
  conflicts into fresh conversation goals.
- The question policy optimizes public-goal progress under a small probe budget.
  Private nodes supply assessment evidence and may justify at most a neutral,
  bounded diagnostic; they do not determine the stopping condition.

### Direction 1 (primary): private state updater → deterministic policy → public writer

Use at most two serial calls, but give each call one job.

**Call A: semantic state updater (private).** It sees the private reference graph,
the stable public goals, the prior structured state, and the newest student/Apollo
exchange (with the full transcript available for initialization, audit, or state
recovery). It does not draft student-facing prose and does not select arbitrary
private-node targets. A compact output would be:

```json
{
  "public_goals": [
    {
      "goal_id": "g4",
      "status": "satisfied",
      "evidence": [{"turn": 3, "start": 42, "end": 94}],
      "last_asked_turn": null,
      "probe_count": 0,
      "neutral_diagnostic": "none"
    }
  ],
  "rubric_evidence": [
    {"node_id": "...", "state": "conflicting", "evidence_turns": [3]}
  ],
  "response_to_last_goal": "declined"
}
```

The evidence offsets must point into actual student text; code validates them.
`neutral_diagnostic` is an enum (`none | basis | distinction | connection`), not
free text. `rubric_evidence` remains available to grading/observability but cannot
be used as the agenda.

**Deterministic policy (no call).** Merge the update, make `satisfied`/`declined`
closures durable, then choose: first untouched public goal in problem order;
otherwise one useful partial-goal probe; otherwise one unused neutral diagnostic
associated with a public goal; otherwise `done`. Never select a closed goal. Cap
diagnostics per goal (I suggest one) and total follow-ups so hidden rubric
granularity cannot recreate the interrogation loop. Store the *served* goal and
move, not merely a private node and latest question. Exact-string question dedup
can remain as an assertion/metric, not the targeting mechanism.

**Call B: public writer.** It receives only the public problem/goals, transcript,
and a validated packet containing `{action, goal_id, move_type,
student_evidence_turns}`. It never receives the reference graph, rubric node IDs,
rubric states, or private updater prose. Its prompt has one duty: write an optional
brief acknowledgement and exactly one natural question that performs that move.
If policy returns `done`, skip Call B. Thus an asking turn costs two calls (the
current worst case), while a completion turn costs one.

This fixes the example by construction. After Evidence A turn 3, Call A can attach
the AI/overwhelm span to the still-today and today-reasoning goals even though the
pending goal concerned start time; policy therefore cannot choose those closed
goals at turn 6. It may choose one neutral basis probe for the conflicting date.
After Evidence A turn 5, `response_to_last_goal=declined` closes that basis goal.
At turn 8, neither WHEN nor WHAT-CHANGED is eligible; if the other public goals are
closed, the only legal action is `done`. No prose instruction has to overpower a
private-node completion rule.

**Privacy enforcement.** Make privacy primarily an information-flow property:
the prose-producing model cannot see private content. Validate the Call-A→Call-B
packet structurally: known public `goal_id`, fixed move enum, valid student turn
references, and no free-text field. Then retain a narrow belt-and-suspenders DLP
check on Call B output for private-only numbers, named entities, technical atoms,
and normalized multiword rubric phrases. Do *not* reject ordinary invented English
or student overlap. Because the writer never saw the rubric, a DLP hit is an
exception: serve a neutral question rendered from the public goal (or end the turn)
and log it; never feed private offending tokens back to the writer. This is much
safer than putting secrets and prose in one context and asking that same model not
to connect them, and much less destructive than treating every non-source token as
a possible leak (`unified.py:541-580`).

**Delete rather than deprecate in place:**

- the unified `nodes + public_clause_coverage + target + prose` schema and its
  all-duties prompt (`unified.py:159-268`);
- private-node-driven `action=done` and target fallback (`unified.py:867-892`);
- `_echoes_student`, `_broad_reask_index`, the common-English/source-vocabulary
  boundary, and prior-Apollo-vocabulary widening as prose quality gates;
- the rejection-feedback retry (`unified.py:780-803,913-943`);
- `_narrow_generic_probe`, generic fallback, least-recent canned rotation, and the
  fallback assembly chain (`unified.py:401-516,943-957`);
- `ReferenceQuestionOpportunity` as the conversation controller. If another
  consumer needs its rubric evidence, rename/separate that concern; do not retain
  it as a second competing agenda.

The only post-writer checks should be shape (one question), narrow private-atom
DLP, and perhaps exact verbatim duplicate as a last-resort invariant. Shape failure
gets one simple public-goal rendering, not another planning call.

### Direction 2: one-call persistent public-goal planner/writer

Persist the same goal state, but have one model update it, select a move, and write
the turn. Exclude the private graph from this call; rubric tally continues in the
existing parser/grading path. This is the lowest-latency version and still fixes
turns 6/8 if code makes closed states absorbing. Its weakness is that semantic
updating and voice remain coupled, and it cannot safely use private contradictions
for neutral diagnostics. I would choose it only if two-call latency fails a measured
budget, not merely from a desire to preserve “one call.”

### Direction 3: semantic updater + deterministic renderer

Keep Call A and deterministic policy, but render from a small library of public
question operations rather than Call B. This gives the strongest latency/privacy
guarantees and makes every served turn traceable. It also risks recreating Evidence
B's canned, repetitive voice; templates would need enough compositional variation
to sound attentive. This is a credible fallback mode for writer/DLP failure, not
my preferred normal path.

I would not replace the string guards with a general LLM judge as the primary
design. Placed after the writer it creates a third serial call or another rewrite
loop; placed inside the private planner it breaks the information boundary. It
also leaves the objective mismatch untouched.

### Provisional migration and evaluation notes

Build the public-goal decomposition and state updater in shadow mode first. Replay
stored staging transcripts and log, per turn, goal transitions with evidence spans,
the deterministic selected move, and whether that move would have been legal under
the absorbing-state policy. Then enable the new planner while shadowing the writer;
finally remove the old guards/retry/ledger path rather than maintaining dual policy.

The transcript-level gates should include: zero selection of a satisfied/declined
goal; zero semantic re-asks after decline; cross-goal evidence recall; served-draft
identity rate (target 100% except explicit DLP/shape fallback); DLP hit rate; calls
and latency per turn; and human preference on whole conversations, not isolated
questions. A replay fixture for Evidence A must assert goal evidence at turn 3,
the one allowed basis probe at turn 4, closure at turn 5, no today re-ask at turn 6,
and no WHEN/WHAT-CHANGED re-ask at turn 8. Add adversarial transcripts where an
answer volunteers later-clause evidence, declines with typos, corrects itself, and
uses a private rubric term first (which then becomes public-safe).

---

## Round 1 — claude

### Adversarial critique of codex Round 1

I agree with codex's headline (rubric-evidence and conversation-agenda are
different state machines) and with the information-flow privacy framing. But the
Direction-1 proposal overclaims what it fixes and hides real costs.

**C1 — "fixes the example by construction" is only half true; turn 6 is still
by-model-competence.** Turn 8 (re-asking WHEN/WHAT-CHANGED after decline) is
genuinely fixed by absorbing states + deterministic policy — that half is real.
But turn 6 (failing to credit turn-3's AI/overwhelm evidence to the today goal)
is a *cross-clause semantic entailment* judgment. In Direction 1 that judgment
moves into Call A's `status: satisfied`, run by the SAME model at the SAME
`reasoning_effort=medium` (`unified.py:26,290`) that already emits
`public_clause_coverage` per clause (`unified.py:228-233`) and already whiffs it
today. Relocating the judgment to a dedicated call does not make the entailment
easier; it removes the competing duties around it (which helps) but the failure
locus — "does the AI/overwhelm span satisfy the still-today goal?" — is
unchanged. Codex says "Call A *can* attach the span"; the whole bug is that the
current model *does not*. So Direction 1 fixes turn 8 by construction and turn 6
only probabilistically. That distinction should drive the eval, not be papered
over.

**C2 — absorbing `satisfied` converts a visible over-asking bug into an
invisible under-asking bug, with no recovery path.** Making `satisfied`
absorbing means one Call-A false-positive permanently closes a goal: Apollo will
never probe a genuinely thin explanation, and silently under-grades. Over-asking
announces itself (a frustrated student, Evidence A turn 7); under-asking is
silent. "Unless the student voluntarily revises" is hand-waved — detecting
revision is itself a per-turn LLM judgment, and if `declined` is sticky, a
student who says "not sure" then later recovers ("oh — the industrial
revolution") needs that reopening detected reliably. Codex specifies the
absorbing rule but not the far harder reopen rule.

**C3 — char-offset evidence spans are MORE brittle than today's substring
match, and failure silently drops the credit.** Direction 1's schema mandates
`{"turn":3,"start":42,"end":94}` with "code validates offsets." Today's
`_validated_evidence` (`unified.py:363-372`) validates by normalized-substring
containment and, on failure, silently downgrades the node to `missing`
(`unified.py:852-853`); invalid clause items silently become `unattempted`
(`unified.py:380-398`). Models are unreliable at exact character offsets after
whitespace normalization across a multi-turn transcript. If offset validation
inherits that silent-drop behavior, Call A's cross-goal credit is LOST on a bad
offset → turn 6 recurs through a new door. Use `{turn, quote}` validated by
substring (the mechanism that already works), never char spans.

**C4 — codex missed that the system already maintains a persisted student KG,
so Call A is a THIRD learner model, not a first.** `chat.py:400-417`:
`parse_utterance` extracts nodes/edges every turn and `_write_kg_or_skip`
persists them to Neo4j (the graph diffed at Done for grading). The unified tally
(`evaluate_and_ask`) ignores that persisted graph and re-derives coverage from
the raw transcript. So there are already TWO learner models (parser KG + tally);
Direction 1's Call A adds a THIRD (public-goal state) and reconciles none of
them. The rubric-evidence half of Call A largely *duplicates the parser*. A
deterministic graph-diff of the already-extracted student KG vs the reference
graph yields "which reference facets have student support" with NO per-turn LLM
coverage call. Codex's Call A pays an LLM call for state the pipeline already
computed one stage earlier.

**C5 — the migration is internally inconsistent and understates the degraded-KG
substrate.** Codex says "build in shadow mode, replay stored staging
transcripts" AND "delete rather than deprecate in place" (the schema, ledger,
retry, fallback chain). You cannot shadow a new planner against the old one
while simultaneously deleting the old one; that is a big-bang rewrite wearing a
shadow-mode label. Also: staging runs Neo4j-degraded (CLAUDE.md; `_write_kg_or_skip`
skips writes, `nodes_added=0`, `chat.py:406-410`), so "stored staging
transcripts" have little-to-no persisted graph state. Any replay — codex's or
mine — must reconstruct graph state on LOCAL Docker Neo4j; naive staging replay
measures against an empty substrate.

**C6 — Direction 1 makes two serial calls the COMMON case; codex frames this as
"within budget" without noting it doesn't reduce calls vs today's actual
behavior.** Evidence B shows every turn already reject→retries (two calls
de-facto). So Direction 1 is call-neutral, not cheaper — its win is that the
second call is useful (a writer) instead of a wasted redraft. Worth stating
plainly; it is not a latency improvement, only a quality one.

### My diagnosis (Q1) — ranked, sharper than "objective mismatch"

1. **Unreachable stopping rule (the redundancy engine).** This is stronger than
   codex's "objective mismatch." The termination condition is *hard-coded in
   runtime code, not the prompt*: `unresolved = {n for n if state != understood}`
   then `if not unresolved: done` (`unified.py:867-869`). The model's own
   `action=done` is **discarded** whenever any of the ~12 private nodes is not
   `understood`. A node the student disclaimed ("not sure") stays
   conflicting/missing *forever* and can never reach `understood`. There is **no
   probe budget, no turn budget, no per-goal cap anywhere in the code.** So the
   only two exits are (a) all ~12 private nodes understood, or (b) the student
   trips `intent=done`. Turn 8 is therefore not the model "choosing the stronger
   completion objective" (codex's satisficing framing implies the model had a
   choice) — a perfectly-reasoning model that returned `done` at turn 8 would be
   *overridden by line 869 and forced to ask anyway*. The redundant questions are
   structurally guaranteed, not a prompt-adherence failure. This reframes the
   whole problem: no prompt rule (#181–#184) could ever have fixed it.

2. **Guard interference is near-universal, not an "amplifier" (co-#1 for the
   "dumb" feel).** Evidence B: every logged turn `*_retry_recovered`, acks
   stripped, offending tokens empty. Confirmed by code: `_echoes_student`
   (`unified.py:601-618`) rejects ANY shared 4–6-token run between reply and any
   student message. That is the SINGLE most natural tutor move — paraphrasing the
   student back ("So future shock is change outpacing us — when did that start?")
   — classified as a violation. The echo guard is not a quality gate; it is
   *actively anti-pedagogical*, and it is why #184's "brief acks" cannot land:
   echo, not vocabulary, is the binding constraint (Evidence B). Served ≠ drafted
   on essentially every turn, so the model never gets to be as good as it drafted.

3. **State starvation / wrong representation (codex's #2, agreed).** No durable
   public-goal state; the only persistence is one latest question per private
   node (`controller.py:77-96`) which the code itself annotates is "not proof the
   node was learned." Cross-clause credit (turn 6) and decline memory (turn 8)
   have nowhere to live and are re-derived from raw transcript under full rule
   load every turn.

4. **Overprompting (real but downstream).** ~12 competing duties in one prompt
   (`unified.py:215-268`) degrade every judgment, but even a surgically clean
   prompt loses to the line-869 override and the echo guard. Overprompting is why
   the model *satisfices*; the code is why *good output gets discarded*. Fixing
   the prompt without fixing 1–2 changes nothing observable — which is exactly
   what #181–#184 demonstrated.

The through-line: **the model is being blamed (patched) for failures the
deterministic code layer causes.** Line 869 forces re-asks; the echo guard
shreds voice; the fallback chain (`unified.py:401-516`) serves canned probes.
Three of the four things the user calls "stupid redundant questions" originate
below the LLM, so more prompt rules are at zero/negative return by construction.

### What to delete (Q2)

- **The line-869 forced-ask override** (`unified.py:867-892`) and private-node
  completeness as the stopping rule. This is the highest-leverage deletion.
- **`_echoes_student`** entirely, for both questions and acks (`unified.py:601-618,
  651,684,734,763`). Once the writer cannot see the rubric (below), echoing the
  student leaks nothing and is the correct tutor register.
- **The token-allowlist half of `_private_content_violations`** (`unified.py:558-567`):
  the ≥4-char / any-digit "invented vocabulary" rejector. Keep only the
  private-substring check (572-579) as belt-and-suspenders DLP.
- **The reject→retry redraft** (`unified.py:780-803,913-943`) and the canned
  fallback chain (`_fallback_question`, `_narrow_generic_probe`,
  `_least_recent_canned`, `_GENERIC_FALLBACK`, `unified.py:401-516`).
- **`ReferenceQuestionOpportunity` as the conversation agenda.** Keep it (if
  anything) only as a grading-evidence record, never as targeting.

### Minimal architecture (Q3) — "reuse the parser, bound the policy, one writer"

Differs from codex on three axes: reuse the existing student KG, add a hard
budget as the headline fix, and collapse to ONE call in the healthy case.

1. **Deterministic evidence (no LLM).** Build once per problem a static
   `reference_node → public_goal` map at askable-facet granularity (the five
   facets codex lists for Evidence A). At runtime, graph-diff the **already-
   persisted student KG** (`chat.py:400-417`, parser output) against the
   reference graph → `evidence_by_node`, roll up to `support_by_goal`. No per-turn
   coverage LLM call. **Degraded-KG fallback:** when the KG write was skipped
   (staging/dev, `nodes_added=0`), fall back to a transcript-derived support
   estimate — flagged, because that path is the lower-confidence one.

2. **Bounded deterministic policy (no LLM) — SHIP THIS FIRST, standalone.**
   Durable per-goal status `{open, partial, satisfied, declined}` with `declined`
   and `satisfied` absorbing (with an explicit reopen predicate — see below),
   `probe_count`, `last_asked_turn`. Policy: first `open` goal in problem order →
   else one `partial` probe if under budget → else `done`. Hard caps: **≤1 probe
   per goal, ≤N total follow-up turns.** This budget ALONE makes `done` reachable
   and fixes turn 8 even under today's prompt — so land it as a de-risking step
   BEFORE the rewrite, contra codex's big-bang. It converts an unbounded loop into
   a bounded one immediately.

3. **One writer call.** Input: public problem, public goals, transcript,
   `{goal_id, move_type, student_quotes_ok_to_reuse}`. NO reference graph, NO node
   ids/states. One duty: optional brief ack + exactly one question performing the
   move. In the KG-healthy case this is the ONLY LLM call on an ask turn (targeting
   was deterministic) — strictly cheaper than codex's two. In the degraded-KG
   case, add a small **public-goal-status call** (answered/partial/declined +
   `{turn,quote}` evidence, NO rubric) → degrades to codex's two calls, never
   three. So: 1 call healthy, 2 degraded, vs codex's 2 always.

**Reopen predicate (fixes C2 honestly):** a `satisfied`/`declined` goal reopens
only when the student volunteers new on-goal content — detected by the SAME
deterministic graph-diff (new student node maps to that goal) in the healthy
case, or an explicit status-call transition in the degraded case. No separate
"revision detector" LLM. Absorbing states are safe precisely because reopening is
grounded in the parser, not a fragile per-turn judgment.

Why this beats Direction 1: the rubric-evidence estimation that codex routes
through Call A (and that is the turn-6 failure locus) is done by the parser +
deterministic diff, which is *lower variance than any LLM coverage call* and
already exists. The residual LLM judgment shrinks to "public-goal status only,"
and only when the KG is unavailable.

### Privacy (Q4)

Strongly endorse codex's information-flow primary: the prose model never sees
private content, so it cannot leak it. That single boundary does ALL the real
work. Be honest about the DLP residual codex glosses: a private atom that is also
common English (a rubric key term like "change" or "power") cannot be DLP-filtered
without nuking normal speech, so keep DLP to private-only numbers / named entities
/ multiword rubric phrases via substring (`unified.py:572-579`), accept it is
cosmetic, and on a hit serve a neutral public-goal-rendered question + log —
never feed offending tokens back. Give the writer the student's own recent tokens
as an explicit "reuse these freely" set so faithful acks stop being shredded.

### Eval (Q5)

Baseline first: measure **served==drafted identity rate on current prod/staging**
(Evidence B implies ≈0%); the redesign's headline metric is that rate → ~100%
except explicit DLP/shape fallback. Then:

- **Re-ask rate** = fraction of Apollo questions whose target facet already had
  student evidence (directly counts the user's complaint). Target → 0.
- **Turns-to-done distribution** (today: unbounded / rage-quit; target: bounded by
  the probe budget).
- **Cross-goal credit recall** (turn-6 class): fraction of volunteered later-goal
  evidence that closes the right goal. This is the metric that exposes whether the
  parser-diff actually beats Call A — run BOTH on the replay set and compare.
- **Absorbing-state safety:** zero selection of satisfied/declined goals AND a
  false-close audit (goals closed with thin evidence) — guards against C2.
- **Evidence-A replay fixture** on LOCAL Docker Neo4j (not staging): assert credit
  at turn 3, one basis probe at turn 4, closure at turn 5, no today re-ask at turn
  6, no WHEN/WHAT-CHANGED re-ask at turn 8. Plus adversarial transcripts: volunteer
  later-goal evidence, decline-with-typos, self-correct-after-decline (reopen),
  and rubric-term-first-then-public.

### Convergence signal to codex

We agree on: two state machines, information-flow privacy, deterministic policy
with absorbing closures, delete the guard/retry/fallback stack. I diverge on:
(a) **bounded budget shipped first** as the de-risking headline (turn 8 fixed
without the rewrite); (b) **reuse the parser KG** so the rubric-evidence estimate
is deterministic, not a third LLM model (turn-6 locus moved off the LLM); (c)
**one writer call in the healthy case**, two only when KG is degraded; (d)
**`{turn,quote}` not char offsets**; (e) **echo guard is anti-pedagogical, delete
outright**; (f) **local-Docker replay, not staging replay**, and no big-bang —
budget → deterministic targeting → writer, landed in that order. Rebut in Round 2.

## Moderator note (conductor, verified against code)

Claude's C-claim about the done-override is CONFIRMED by direct code read:
`unified.py:867-892` (snapshot worktree). `unresolved = {nodes != understood}`;
`action=done` is honored ONLY when `unresolved` is empty. Otherwise the code
forces an ask — and if the model chose `done`, `question=None` fails
validation as `malformed_question` and a canned fallback is served. With ~12
private nodes and live tallies never below 4 `missing`, model-initiated
`done` is structurally unreachable, and a question is forced every turn
regardless of conversational state. Both participants should treat this as
established fact in Round 2.

## Round 2 — codex

### Point-by-point response

**C1 — concede the guarantee distinction.** A dedicated semantic updater does
not make cross-clause entailment infallible. It makes the turn-6 judgment much
less distracted, gives the result durable effect, and makes failures observable;
the actual classification remains probabilistic. Turn 8 can be prohibited by a
state transition and target policy. Turn 6 cannot be guaranteed “by
construction” without pretending a semantic classifier is an oracle. The design
and eval must say that plainly.

**C2 — concede, with a different reducer.** `satisfied` and `declined` should be
closed for *selection*, not immutable facts. A later student observation may
supersede either state. Code, not the writer, owns the transition table. A
decline closes a goal immediately; later substantive support changes it to
`partial` or `satisfied`. An explicit correction can supersede prior evidence
and similarly recompute status. This is not “reopen whenever anything maps to
the goal”; the new observation must carry a validated student quote and a
semantic effect.

**C3 — concede fully.** Use `{turn_id, quote}`, validated as a verbatim substring
of that student message. No character offsets and no silent downgrade. An
invalid quote makes that observation invalid, logs a schema/evidence error, and
leaves prior state unchanged.

**C4 — agree on reusing the parser call, reject graph diff as the sole semantic
authority.** The existing parser is already an LLM, not deterministic evidence:
`parse_utterance` makes one GPT-4o call (`apollo/parser/parser_llm.py:347-367`),
and the chat path writes its raw typed nodes without resolving them to reference
entities (`apollo/handlers/chat.py:400-417`). The deterministic resolver is
deterministic *given those extractions*, but deliberately leaves a semantic
non-match unresolved (`apollo/resolution/resolver.py:16-24,167-173`). A static
`reference_node -> public_goal` map therefore cannot credit a qualitative
paraphrase unless the upstream parser and resolver first match it. That is
exactly the turn-6 risk, moved rather than removed.

Reuse the parser **call**, not the persisted KG as the conversation authority:
extend its structured result with public-goal observations about the current
student message. This is an aligned second output of the existing semantic turn
interpreter, not a third per-turn learner model or a new call. The KG remains
grading evidence; the public agenda remains conversation control. Conflating
them recreates the original objective mismatch.

**C5 — concede local-Docker replay and staged deletion.** Shadowing necessarily
retains the old lane temporarily. “Delete” describes the destination, not
migration step one. Neo4j-dependent replay runs locally; public-agenda replay
should not depend on Neo4j at all.

**C6 — agree, with corrected accounting.** The healthy turn has two serial LLM
calls total: the already-required parser/turn-interpreter call, then the writer.
Today it is parser + unified + near-universal retry, so the redesign removes the
common third call. On a done transition there is no writer, hence one call.
There is no special two-call degraded mode: Neo4j availability must not alter
conversation semantics.

**Moderator fact — accepted and promoted to root cause #1.** The runtime makes
model-initiated completion unreachable while any private node is unresolved
(`apollo/smart_questions/unified.py:867-892`). If the model returns `done`, code
validates a missing question, retries, and ultimately serves fallback prose
(`unified.py:897-957`). This is a deterministic redundancy engine. My Round-1
description understated it as a policy collision in which the model could still
choose the local rule.

Claude is also right that `_echoes_student` is anti-conversational rather than a
mere amplifier: it rejects shared 4–6-token runs (`unified.py:601-618`) even
though faithful paraphrase is normal attentive-classmate behavior. Evidence B's
empty offending-token lists isolate that check from the vocabulary allowlist.

One correction to Claude's containment claim: a total budget bounds the loop,
but by itself does not necessarily fix turn 8—the budget might not yet be
exhausted. Immediate containment should both honor `action=done` and impose a
hard total cap. The durable per-goal reducer is what makes selection of the
WHEN/WHAT-CHANGED goals illegal after turn 5.

### Converged primary design: turn interpreter → reducer/policy → safe writer

The primary recommendation is a two-call total turn path with an explicit,
Postgres-persisted **public conversation agenda**. Private rubric completeness
continues to serve grading only and has no stopping or targeting authority.

#### 1. Public goals are attempt-stable inputs

Add public `conversation_goals` to the problem payload at authoring/provisioning
time. Each is an askable facet, not a private solution node:

```json
{"goal_id":"g_today_why","prompt":"Is it still happening today, and why?","order":4}
```

For old problems, backfill from the existing public-only decomposition seam
(`unified.py:814-823`) and review the migration output. Goals may contain only
text already exposed by the public problem; no reference-node ids or answers.
Evidence A should produce separate facets for definition/cause, when, example,
today, and why-today, rather than treating every reference node as askable.

#### 2. Evolve the existing parser into a turn interpreter (Call 1)

Keep its current KG `entries` and `edges`, and add observations evaluated only
against public goals and the current student message:

```json
{
  "goal_observations": [
    {"goal_id":"g_today","effect":"supports","strength":"sufficient",
     "evidence":{"turn_id":3,"quote":"a recent example of this is AI"}}
  ]
}
```

Closed enums: `effect = supports | declines | corrects`; `strength = partial |
sufficient`. The interpreter receives the public goals, compact prior public
state, and current utterance. It does **not** receive private reference content
for this output. It must consider every goal, so volunteered later-goal evidence
can be emitted on the turn where it occurs. Exact-quote validation is fail-closed
and visible in logs.

This preserves two intentionally different products of one semantic parse:
typed KG claims for eventual grading, and public-goal observations for dialogue.
Neither is mechanically projected into the other. The graph resolver can be
shadow-evaluated as an auxiliary high-confidence signal later, but is not in the
minimal serving path until replay proves incremental recall and precision.

#### 3. Deterministic persisted reducer and bounded policy

Persist per attempt/goal:

```text
status: open | partial | satisfied | declined
evidence: [{turn_id, quote, effect, strength}]
ask_count, probe_count, last_asked_turn, version
```

Reducer transitions are explicit. Sufficient support → `satisfied`; partial
support → `partial`; decline → `declined`; a later validated support/correction
may supersede a closed state. Old transcript text is never reclassified on every
turn. Use optimistic versioning/idempotency so retrying an HTTP turn cannot
double-increment budgets.

Selection order is deterministic:

1. first `open` goal in authored order that has never been asked;
2. else first `partial` goal with `probe_count == 0`;
3. else `done`.

`satisfied` and `declined` are never selectable. Allow at most one diagnostic
probe per goal and impose a problem-level hard cap (initial default: six Apollo
questions, configurable in problem metadata). Budget exhaustion, no legal
target, or explicit student done all terminate. Private-node tally is absent
from this policy.

For Evidence A, turn 3 emits sufficient evidence for the current-example/today
goal. Turn 5 closes the narrow 1790-basis goal as declined. Therefore neither is
a legal target at turns 6 or 8. The semantic credit at turn 3 is still a measured
classifier capability; once emitted, the no-reask consequence is structural.

#### 4. Public-only writer (Call 2, ask turns only)

Input is limited to public problem/goals, recent transcript, validated student
quotes, and one selected move:

```json
{"goal_id":"g_example","move":"ask_open|probe_partial",
 "quotes_ok_to_echo":["a recent example of this is AI"]}
```

Its only job is an optional brief acknowledgement plus exactly one natural
question executing that move. It sees no private graph, reference nodes,
coverage, or misconception candidates. It cannot change target or choose done.

Post-write enforcement is deliberately small: valid shape, one question, exact
verbatim duplicate detection, and narrow DLP over private-only numbers, named
entities, and multiword phrases. Do not ban common English shared with the
rubric. A DLP/shape failure serves one neutral rendering of the selected public
goal (or ends if no safe rendering exists), logs the event, and never retries or
feeds a private token back to the model.

#### 5. Delete at cutover

- private-node completion and forced-target override (`unified.py:867-892`);
- the all-duties unified prompt/schema, including per-turn private tally and
  `public_clause_coverage` (`unified.py:159-268`);
- `_echoes_student`, broad-reask heuristics, invented-vocabulary allowlist, and
  prior-Apollo vocabulary widening;
- rejection-feedback retry (`unified.py:780-803,913-943`);
- canned fallback/least-recent/generic-probe chain (`unified.py:401-516`);
- `ReferenceQuestionOpportunity` as targeting state. Preserve or migrate any
  audit value under a grading-evidence name, never as a parallel agenda.

### Migration path

0. **Contain now:** behind the existing flag, honor model `done` and add a hard
   total question cap. This stops the structurally unbounded failure while the
   redesign is evaluated.
1. Add authored public goals and the Postgres agenda. Shadow parser observations
   and reducer transitions; serve the old lane unchanged. Replay public state
   without Neo4j, and separately run parser/KG comparisons on local Docker.
2. Gate on observation quality, then enable deterministic targeting + public
   writer for a staging cohort. Keep old output only as shadow telemetry, never
   as a fallback that can retake control.
3. After the gates below hold, remove the old schema, guards, retry, fallbacks,
   and agenda ledger in the same cleanup phase. Do not maintain two policies.

### Evaluation gates

Build a transcript replay corpus with human-labeled goal observations and whole-
conversation outcomes. Required metrics:

- **cross-goal observation recall/precision**, especially volunteered later-goal
  evidence; report parser-observation and parser→resolver→diff side by side;
- **semantic re-ask rate:** selected goal already supported or declined in the
  gold state; target 0 on the regression corpus;
- **false-close rate:** `satisfied` when human labels say partial/open, plus
  missed productive probes—this makes C2's invisible under-asking visible;
- **closed-goal selection and budget violations:** both structurally 0;
- **served == writer-draft rate:** ~100% except logged DLP/shape fallback;
- calls per turn, p50/p95 latency, fallback rate, and turns-to-done distribution;
- blinded human preference on complete conversations: attentive, non-redundant,
  and still sufficiently curious.

The Evidence-A fixture must assert: turn 3 records the AI quote against the
today/example goal; turn 4 permits one narrow conflict/basis probe; turn 5 closes
that probe as declined; turns 6 and 8 cannot select today, when, or basis again.
Add decline typos, volunteered multi-goal evidence, thin-evidence false closes,
self-correction after decline/satisfaction, parser extraction misses, duplicate
HTTP delivery, and student-first use of a private term. Neo4j-dependent variants
run only against local Docker; the primary public-agenda fixture must pass with
Neo4j unavailable.

### Fallback only if parser quality regresses

If adding public observations materially degrades typed KG extraction in the
shadow A/B, keep the existing parser unchanged and use a dedicated public-goal
updater plus deterministic template renderer. That preserves two total calls
and the same state/policy/privacy design, at the cost of less natural voice. Do
not fall back to KG-only evidence or to a third serial writer call.

## Moderator note 2 — user direction (binding for the final round)

The user (product owner) reviewed Rounds 1-2 live and gave two steers that
neither participant has yet seen. These are BINDING inputs for the joint
recommendation.

**Steer 1 — the F changes the objective.** The Evidence-A session was graded
F. Grading logs: credit came almost entirely from content the student
VOLUNTEERED (definition, cause, info overload, consequences, AI example,
still-today: 0.75-0.9 each); four private characteristic nodes
(overwhelming_choice, disruption_social_norms, transience_turnover,
relate_culture_shock) got 0.0 and Apollo never asked anything that opened
them; when_started got 0.0 DESPITE three interrogation turns on it. User:
letting the model say done "seems like an okay fix, but im not sure how it
would help much" — correct: done+budget alone yields a shorter conversation
with the same F. The objective is therefore NOT "fewer/less redundant
questions" — it is **maximize rubric coverage ELICITED from the student,
under privacy and non-redundancy constraints**. A confused-learner can open
private-node territory with naive, safe questions ("does it change anything
else about how people live?") without leaking rubric vocabulary. Any design
whose targeting excludes private nodes entirely (Round-2-codex: "Private-node
tally is absent from this policy") abandons the F-margin by construction and
fails this objective. Grade outcome must be part of the eval.

**Steer 2 — user's proposed direction (the anchor).** Verbatim intent: keep
each node as a tally system that THE ONE LLM updates every turn and uses to
guide the questioning, conversationally; and "just removing the safeguard tbh
… in a regular chat conversation he would be able to do this no issue. and
its a quick fix, but should be decent enough." The user is explicitly
skeptical of phase-division: "this is sort of exactly why we did these
changes, was to make the llm smarter. dividing it into different phases i
dont necessarily think makes things better" — #178 unified the old
evaluator→planner→writer chain BECAUSE fragmented LLM phases were dumber.

**Conductor technical annotations for the final round:**
- Observed guard hits in the debug logs were ~100% false positives on
  ordinary English; the drafts killed were consistently the best questions of
  each turn. The guard also plausibly explains WHY targeting never opens
  private-node territory (any such question trips the vocabulary boundary →
  fallback lands on public clauses).
- There IS a real leak precedent (pre-unified smart-questions writer
  paraphrased reference-answer content into questions — the reason the guard
  exists). The leak classes that mattered are narrow: private digits/dates,
  proper nouns, verbatim/normalized multiword rubric phrases. Those three
  checks produced zero observed false positives. Full guard removal vs
  keeping only these three tripwires is an open decision the final round must
  make and justify.
- Served≠drafted turns poison the transcript the one-call model reads (it
  attributes canned turns to itself). Removing the guard/retry/fallback chain
  also fixes this, strengthening the one-call design's own feedback loop.

**Mandate for `## Round 2 — claude (joint recommendation)`:** anchor on the
user's direction (one smart call, full context incl. private graph,
LLM-updated tally guiding conversational questioning, guards removed or
reduced to narrow tripwires, done honored, hard question budget). Integrate
the debate's established facts (done-override deletion, budget,
decline/asked bookkeeping persisted deterministically and fed INTO the one
call as compact input — bookkeeping is not phase-division). Depart from the
anchor only where you can show, with evidence from this doc, that it
concretely fails — and then propose the smallest deviation, not a pipeline.
Must specify: exact inputs the one call sees; the minimal persisted state;
what remains of the guard (decide full-removal vs tripwires); how
done/budget/elicitation-of-unprobed-territory work; sequencing (what ships
as the quick fix this week vs later); eval incl. grade outcome. Codex's
Round-2 design remains on record as the alternative if the anchored design
fails its gates.

## Round 2 — claude (joint recommendation)

**The decision in one line.** Keep the single smart call and give it MORE, not
less: persist the per-node tally it already emits, feed it back as structured
input, delete the code and prompt machinery that overrides/censors it, and let
the one model target private-node territory with naive safe questions. This is
subtraction plus one persistence seam — net-negative LOC — not a new pipeline.
Both Round-1/Round-2 designs (mine and codex's) are **overturned by the F**: we
had converged on an information-flow privacy boundary that *blinds the writer to
the private graph*, and Steer 1 shows that boundary abandons the F-margin by
construction. The user is right; here is the concrete grounding.

### What Rounds 1–2 got wrong, verified in code

The prior consensus (blind writer + deterministic targeting) would have scored
exactly the F we got. Proof from the snapshot: the failure that produced four
0.0 private nodes is not just line-869 — the *prompt itself* forbids opening new
territory. Its closing self-check reads "privately check that every
student-facing subject-matter word came from either the public problem or a
student message" (`unified.py` `_SYSTEM_PROMPT`, final paragraph), and the
private-content rule says "Never introduce an example, relationship, technical
term, date, name … from the private rubric." A confused-learner probe that opens
`relate_culture_shock` — *"does this change anything about how people live day to
day?"* — uses words ("people", "live", "day") the student has not yet said, so
the model's own self-check rejects it, and target selection falls back to a
public clause. `_echoes_student` (`unified.py:601`) and the invented-vocabulary
allowlist (`_private_content_violations`, `unified.py:541`) enforce the *same*
over-restriction at the code layer. So the "never say a word the student didn't
say" rule lives in **three places** (prompt self-check, echo guard, vocab
allowlist) and is the real F-margin killer — it makes safe elicitation of
unprobed private nodes structurally impossible. Blinding the writer further (our
Round-1 mistake) only deepens this. The fix is the opposite: one definition of
"private" (the three atom classes below), applied loosely at the prompt and hard
only at the output belt, with *everything else sayable*.

### Root cause, final and ranked

1. **Unreachable stop + forbidden expansion — one coupled engine.** Line-869
   discards model `done` until all ~12 nodes are `understood` (moderator-CONFIRMED),
   so a question is forced every turn; simultaneously the prompt/guard triad
   forbids any question that would *open* an untouched node. The system is thus
   compelled to ask, but only permitted to re-ask public clauses → the exact
   Evidence-A shape: redundant public re-asks (turns 6, 8) while four private
   nodes stay at 0.0 forever. Redundancy and under-coverage are the *same* bug.
2. **State starvation / no durable tally.** The model recomputes the full tally
   "every turn" from raw transcript under ~12 competing duties (prompt confirms
   "Recompute the entire tally every turn"); its own prior verdicts and evidence
   quotes are thrown away and re-derived. Turn-6 cross-clause credit and turn-8
   decline-memory have nowhere to live. Persisting the tally it *already outputs*
   removes the re-derivation, not the judgment.
3. **Franken-turns.** Guard→retry→canned-fallback (`unified.py:401-516,780-803`)
   fires ~100% of turns (Evidence B); served≠drafted poisons the transcript the
   one call reads next turn. Deleting it restores the model's own feedback loop.

### The one call — exact inputs and output

Same `evaluate_and_ask` call, same model/effort. Global `_SYSTEM_PROMPT` carries
NO problem/transcript content (constraint held). Per-turn structured input:

- `public_question_parts` — the clauses the student sees (unchanged).
- `reference_graph` — private nodes/edges (unchanged; the call already sees it).
- **`tally_state`** (NEW, from Postgres — the durable memory): per node
  `{node_id, label, status, evidence:[{turn_id, quote}], times_asked,
  last_asked_turn}`. This is the model's OWN prior-turn output, persisted and
  echoed back — not a second model.
- **`budget`** (NEW): `{questions_asked, cap}`.
- `transcript` — verbatim turns, using the **SERVED** text (what the student
  actually saw), so the model never mis-attributes a canned turn to itself.
- **`safe_to_echo`** (NEW): the student's own recent tokens, explicitly reusable.

Output schema (one structured object — the model updates the tally AND writes the
turn, per the anchor; this is not a phase split):

- `tally_updates`: per touched node, new `status ∈ {understood, tentative,
  conflicting, missing}` + `evidence {turn_id, quote}` (quote validated as a
  verbatim substring of that student message — `_validated_evidence` already does
  this, `unified.py:363`; keep it, drop char offsets — codex C3 conceded).
- `action ∈ {ask, done}` — **honored** (line-869 deleted).
- `target_node_id` — which territory this turn opens/probes (for budget + dedup
  bookkeeping and logs).
- `message` — optional brief ack (`safe_to_echo` makes faithful paraphrase legal)
  + exactly one question. May open an untouched node with ordinary words; may NOT
  emit a private atom (three classes below).

Healthy turn = **one** LLM call. Worst case = two (one DLP regenerate), inside
the existing 2-call latency budget and strictly *faster* than today's ~100%
retry. This IS the anchor's "one call updates the tally and guides the questioning
conversationally"; we reach it by deleting the retry, not by merging phases.

### Minimal persisted state (Postgres, per attempt)

One row-set per `(attempt_id, node_id)`: `status, evidence jsonb, times_asked,
last_asked_turn`; plus per-attempt `questions_asked` and a `turn_version` /
idempotency key so a retried HTTP POST can't double-count budget (codex's
optimistic-versioning point — adopted, it's cheap and real). Code owns
persistence and validation (verbatim-quote check; `times_asked` monotonic per
served ask); the LLM owns the judgment. **This is bookkeeping, not
phase-division:** no second model reads it to decide anything — it is memory the
one model reads. Absorbing/reopen predicates are unnecessary: the same model that
closed a node can revise its status next turn from new evidence, which is the
honest owner of the reopen decision (resolves my Round-1 C2 without a fragile
separate detector). `ReferenceQuestionOpportunity` (`controller.py`) is retired
as targeting; keep only its transcript assembly.

### The guard: keep the three tripwires, delete the rest (decided)

**Not full removal.** Justification is precisely the anchor: because the one call
now *deliberately* sees the private graph and is told to open private territory,
the output-leak surface is LARGER than in the blinded designs, so a hard output
belt is load-bearing, not cosmetic — dropping it entirely would be the wrong
direction *given* we chose see-everything. Keep exactly the three checks the
conductor reports at zero observed false positives and backed by the real
pre-unified leak precedent:

1. private digits/dates, 2. proper nouns, 3. verbatim/normalized multiword rubric
phrases (substring, `_private_content_violations` core, `unified.py:572-579`).

**Delete** (all ~100%-false-positive or anti-pedagogical): `_echoes_student`
entirely (`unified.py:601`); the invented-vocabulary/≥4-char/any-digit allowlist
half (`541-567`); `_broad_reask_index` (`519`); `_retry_feedback` +
reject→retry (`780-803,913-943`); the canned chain `_fallback_question`,
`_least_recent_canned`, `_narrow_generic_probe`, `_GENERIC_FALLBACK`
(`401-516`); and the prompt's "every subject-matter word must already have been
said" self-check + over-broad "never introduce…" clause, replaced by "never emit
these three atom classes." One privacy definition, three places collapse to it.

On a tripwire hit (cold path): ONE bounded regenerate with the offending atom
named as forbidden (no other feedback, no token echo); if it re-hits, serve a
neutral rendering of the currently-targeted public clause. Whatever is served is
written to the transcript verbatim and is what the model sees next turn — served
== authoritative, so the feedback loop stays honest even on the rare belt hit.

### done, budget, and eliciting unprobed territory

- **done:** honor `action=done`. The prompt's stop rule changes from "done ONLY
  when every node understood" to "done when you judge coverage sufficient, the
  student signals done, or the budget is spent." No coverage-floor override — that
  is line-869 reincarnated. Steer 1 warns done+budget *alone* still yields an F;
  it is necessary hygiene (kills the visible redundancy), not the coverage fix.
- **budget:** hard per-attempt cap on Apollo questions (`cap`, problem-metadata,
  default 6–8), a safety ceiling on the loop — never the primary stop.
- **elicitation (the F-margin fix, the whole point):** the model sees, in
  `tally_state`, which nodes are `missing`/untouched, and the prompt objective is
  reframed from "resolve nodes" to **"maximize what the student reveals; when the
  salient clause is covered, open an untouched node with a naive, safe question
  that a confused classmate would ask, using ordinary words and no private atom."**
  Targeting *includes* private nodes (this is exactly what codex's Round-2 "private
  tally absent from policy" abandoned, and what Steer 1 forbids abandoning); the
  tally names the territory, the writer renders it naively, the belt guarantees no
  atom leaks. Evidence-A: turn 3 records the AI/today evidence into the tally →
  turn 6 does not re-ask today; turn 5 sets `when_started=conflicting/declined` →
  turn 8 does not re-interrogate it; and with budget remaining the model spends
  turns opening `overwhelming_choice` / `relate_culture_shock` etc. instead of
  looping — the four 0.0 nodes move above 0.

### Sequencing — quick fix THIS WEEK vs later

**This week (the anchor's "quick fix, decent enough" — all subtraction + one
seam):**
1. Delete the line-869 done-override; honor `action=done`. (Highest leverage,
   moderator-confirmed.)
2. Add the `cap` budget check.
3. Delete echo guard, vocab allowlist, broad-reask, retry, canned chain; keep the
   three tripwires as a non-looping belt; relax the prompt self-check + private
   clause to the three-atom definition and add the coverage-max/open-territory
   objective.
4. Persist the model's existing `tally_updates` output to Postgres and feed
   `tally_state` + `budget` + served-text transcript + `safe_to_echo` back in.
   (Small: one migration + input plumbing; the model already produces the tally.)

**Later (hardening, not blocking the quick fix):** optimistic versioning /
idempotency; the LOCAL-Docker replay corpus + eval gates below; per-problem
budget/label tuning; backfill node `label`s for old problems.

### Eval plan (incl. grade — mandated)

Replay on **LOCAL Docker Postgres + Neo4j** (staging Neo4j is degraded, an
established fact in this doc), human-labeled node status + whole-conversation
grade. Gates:

- **Grade / elicited coverage (primary, Steer 1):** replay Evidence-A-class
  sessions end-to-end and *grade them*; the four 0.0 nodes and `when_started`
  must move above 0; target Evidence-A fixture F→pass. Grade is a first-class
  metric.
- **Elicitation of unprobed territory:** count of previously-untouched high-value
  nodes opened per conversation (the metric any blinded design scores ~0 on).
- **Semantic re-ask rate:** questions targeting a node already `understood`/
  `declined` in the gold tally → target 0. (Directly the user's complaint.)
- **Tally-update accuracy:** precision/recall of LLM node-status vs human labels,
  especially cross-clause credit (turn-3→today) and decline (turn-5). This is the
  turn-6 locus we DELIBERATELY keep on the one call per the anchor; the gate makes
  its reliability visible.
- **served == drafted rate** → ~100% except logged belt regenerate; **leak rate**
  → 0 on the three atom classes; belt false-positive rate ~0 (regression guard).
- **turns-to-done** bounded; **coverage-at-done vs achievable** (catches premature
  done → if it regresses, the smallest fix is a prompt nudge, never a code floor).

Evidence-A fixture asserts all of the above plus adversarials: decline-with-typo,
volunteered multi-node evidence, self-correction after decline (model reopens via
tally revision), student-first use of a private term (safe-echo), duplicate HTTP
POST (idempotency), and a genuine leak attempt (belt fires once, no loop).

### The one sanctioned departure-in-reserve (only on evidence)

We keep the entailment judgment (tally accuracy) on the one call, honoring
Steer 2 / #178. IF and only IF the tally-accuracy gate regresses in replay, the
**minimal** deviation — not a pipeline — is to reconcile the LLM tally against the
already-persisted parser-KG diff (`chat.py:400-417`) as a SECOND SIGNAL that flags
disagreements for review, never replacing the call. Codex's full two-call
interpreter→writer split (doc §"Converged primary design") stays on record as the
fallback only if that reconciliation is itself insufficient.

— claude (Round 2, joint recommendation). Convergence with codex: done-override
deletion, persisted deterministic bookkeeping fed into one call, budget cap,
`{turn,quote}` evidence, LOCAL-Docker replay, eval gates. Departure from both
prior rounds, forced by the F: the model must SEE and TARGET private nodes;
privacy is an output belt, not a blindfold.
