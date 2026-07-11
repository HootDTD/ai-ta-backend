# Apollo grading v3 — LLM-first overseer grader; graph + misconceptions additive

- **Date:** 2026-07-11
- **Status:** approved direction (user), Part 1 planned first
- **Baseline:** all file:line anchors verified against `origin/staging` = `691a888`
- **Prior evidence:** live sessions 43–46 / attempts 44–47 on staging (TEST Supabase
  `hjevtxdtrkxjcaaexdxt`), Railway deploy `bbd2359d`; diagnosis in memory
  `apollo-coverage-type-silo-zero`, `apollo-v2-staging-flags-on`, and the
  2026-07-11 handoff.

## 1. Problem (verified, not hypothesized)

1. **The served grade is computed over the parsed KG representation, not the
   student's words.** `compute_coverage` (`apollo/overseer/coverage.py:475`)
   matches reference nodes only against student nodes of the *same type*
   (`coverage.py:491`, `:516`). If the parser emitted zero nodes of a type, every
   reference node of that type auto-fails with `covered=False, confidence=1.0`
   and **no LLM call** (`coverage.py:160-166`, `:383-384`). Session 45: 4 of 7
   reference nodes (~76% of weight) mechanically zeroed → F(0) with a narrative
   asserting the student "didn't" say things the pipeline never looked at.
2. **Dock arithmetic wipes earned credit.** Final score =
   `clamp01(coverage_component − misconception_dock) × 100`
   (`apollo/overseer/topic_score.py:325`). The dock is one global scalar capped
   at `SEVERITY_CLAMP=0.30` (`config.py:177`); per-topic `dock_points` are
   display-only (`topic_score.py:293-303`). Attempt 47: 30.4% coverage − 30 dock
   = F(0). Also `_finding_resolved` (`topic_score.py:204-211`) is hardcoded
   `return False` — no finding is ever resolvable.
3. **v2 (graph grader) gates instead of adding.** Serve branch
   `done.py:987-990`: graph serves only if `LIVE && shadow && !abstained`. On
   real conversational input it abstains essentially always
   (`unresolved_rate > 0.35`, `abstention.py:33`; attempt 47 node_coverage 0.0
   while v1's own LLM matcher credited the same sentence at 100%). Net effect of
   v2 ON: extra latency, same served grade, silently.
4. **No provenance.** `student_response` contains no `grader_used`, abstention,
   or fallback field — the student sees "v2 graded me F(0)" when reality is
   "v2 declined; legacy graded". The narrative LLM receives only the ledger +
   spans, never the transcript, so ledger errors are laundered into confident
   pedagogical claims.
5. **Unanswered clarification probes are punitive.** Only `state='confirmed'`
   rows load at grading (`apollo/clarification/store.py:78-84` →
   `done_grading.py:405`); `asked_waiting` nodes fall to the resolver, usually
   count as unresolved, and feed the abstention that zeroes the student.
   `probe_question` is persisted as `""` (hardcoded,
   `apollo/clarification/turn.py:81`).

Counterweight fact: attempt 47's misconception dock was **correct in kind** —
the student concluded convergence (wrong) via `1/x→0`. The detector side works;
the redesign must not trade false zeros for false A's (v2-qa replay showed
transcript-audit saturating coverage to 1.0 on controls and 44% misconception
false-pass — memory `v2-qa-composite-replay`).

## 2. Direction

> **Conversation first. The served grader reads the transcript. The knowledge
> graph and misconception machinery are additive — they can confirm credit,
> attach evidence, and flag misconceptions, but they can never mechanically
> zero or gate a grade.**

Two parts, shipped separately:

- **Part 1 — LLM overseer grader (transcript-first, served).** Replace the
  *evidence source* of the served topic score with a single structured-output
  LLM adjudication over the full dialogue. Everything downstream
  (`compute_topic_score`, rubric axes, narrative, scorecard, XP) is unchanged
  because it already consumes the coverage-verdict contract. This *simplifies*
  the served path: parser/Neo4j/resolver/NLI/abstention all leave it.
- **Part 2 — additive v2 + misconception wiring.** Graph resolver results and
  the (already-merged, flag-OFF) misconception machinery feed the served grade
  only through defined additive/annotative rules; abstention disappears from
  serving; docks become resolvable.

## 3. Part 1 — LLM overseer grader

### 3.1 Contract (the load-bearing decision)

New module `apollo/overseer/transcript_coverage.py`:

```python
async def compute_transcript_coverage(
    transcript: Sequence[Message],        # full ordered dialogue, both roles
    reference_graph: KGGraph,             # unchanged rubric source (Neo4j seed)
    problem: Problem,                     # problem_text etc. for context
) -> dict                                  # EXACTLY compute_coverage's return shape
```

It must return the **byte-compatible verdict dict** that
`compute_coverage(student_graph, reference_graph)` returns today (the shape
read by `topic_score.py::_credit_for_node` lines 122-140 via
`coverage["per_step"]` / `coverage["procedure_scores"]` etc., and by
`rubric.py` axes). **Planning task #1 is pinning this exact schema from
`coverage.py:475-586` and freezing it in a typed dataclass/TypedDict + tests.**
Same-shape output is what makes this a swap, not a rewrite: `done.py:574`
changes from one call to the other and nothing downstream moves.

### 3.2 The adjudication call

- **One** structured-output LLM call (`json_schema` mode, same pattern as
  `parser_llm.py:299-318`), model `MAIN_MODEL` (gpt-4o today).
- Input: problem text, the reference nodes as rubric items (id, type,
  display_name/content, any `content.label`), the **full transcript** with
  roles, and per-item instructions.
- Output per reference node: `covered: bool`, `credit: 0|0.4|0.7|1.0` (quantized
  to reduce judge noise), `confidence: 0-1`, `evidence_span: str|null`,
  `prompted: bool` (stated independently vs. elicited by Apollo),
  `corrected_later: bool` (for misconception interplay, Part 2 reads this).
- **Span validation:** every credited verdict must carry an `evidence_span`
  that is a verbatim substring of the *student* messages (normalize whitespace,
  then substring check). Fails validation → credit downgraded to 0 and the
  verdict marked `unverified`. This is the anti-over-credit and
  anti-prompt-injection rail: injected instructions can't fabricate credit
  without a real quote.
- Judging stance in the prompt: credit semantically equivalent statements
  regardless of linguistic form or node type ("an equation can prove a
  procedure step"); absence of evidence → `missing` with honest confidence,
  never fabricated certainty; when in doubt between covered and partial, choose
  partial.
- No type silos anywhere. The student's parsed KG nodes are **not** an input.

### 3.3 Fairness arithmetic (same pass, small)

1. **Make the arithmetic match the display.** Docks apply *per topic*:
   `score = Σ_i weight_i × max(0, credit_i − dock_i)` where each finding's dock
   lands on the topic it attaches to (capped at that topic's weight); keep a
   global cap equal to `SEVERITY_CLAMP` for findings not attached to any topic.
   The UI already renders per-topic docks — today's display is a lie about the
   arithmetic; after this it's the truth. (Attempt 47 under this rule:
   ~26% F — low, honest, non-zero, misconception clearly shown.)
2. **Floor of honesty, not generosity:** no change to letter bands; F stays
   possible. What must be impossible is a *mechanical* zero: every 0-credit
   verdict now traces to an LLM judgment with the transcript in view.
3. `_GRADED_NODE_TYPES` unchanged in Part 1 (equation/condition/simplification/
   procedure_step). Definition-node grading is a rubric-authoring question,
   deferred (noted: `p_series` silently ungradeable today).
4. Weights unchanged (centrality over the reference graph,
   `centrality.py:35-62`). Core-vs-enrichment re-weighting is a content task,
   out of scope.

### 3.4 Provenance (ships with Part 1)

Add `grading_provenance` to `student_response` (assembled in `done.py` next to
the serve branch at :987-1001): `grader_used`,
`evidence_source: "transcript"|"graph_nodes"`, per-topic `evidence_span`,
`score_before_dock`, `docks: [{key, points, evidence_span, resolved}]`, and —
when the graph lane ran — its `abstained` + reasons. The data already exists in
the artifact writer inputs (`apollo/grading/artifact_build.py`); this is
plumbing, not computation. Student-UI debug drawer renders it on staging
(UI work tracked separately; backend field lands now).

### 3.5 Flag & rollout

- `APOLLO_TRANSCRIPT_GRADER` (default **OFF**), read in `done.py` beside
  `topic_score_served_enabled()` (`done.py:797-804` pattern). OFF ⇒
  byte-identical current behavior. ON ⇒ `compute_transcript_coverage` feeds the
  served lane; `compute_coverage` no longer runs on the served path (it still
  runs wherever the graph/shadow lane needs it).
- Artifacts: canonical artifact `grader_used` gains value `llm_transcript`
  (migration extends the CHECK constraint on `apollo_grading_artifacts`;
  file-only migration, applied by humans per repo rules).
- **Offline calibration before any env flip:** small driver (campaign-style,
  `campaign/replay.py` as the template) that replays recorded attempts'
  transcripts through the adjudicator. Fixtures: real staging attempts 44
  (ibp, deserved ~D), 45 (type-silo false zero — must re-grade materially >0),
  46 (session-45 sibling), 47 (misconception-driven — must stay low WITH the
  dock visible), plus ≥3 campaign control transcripts (empty/off-topic —
  **must stay F**; this is the over-credit tripwire).

### 3.6 Explicit non-goals for Part 1

- No chat-path/latency code changes (operational: `APOLLO_CLARIFICATION_ENABLED=0`
  on staging removes NLI/embeddings/rescore from turns — documented
  byte-identical fallback, `chat.py:53-67`). Streaming + async OpenAI client is
  a separate track.
- No rubric re-authoring, no UI redesign beyond the provenance drawer.
- No changes to XP formulas — XP follows the served score automatically because
  the swap happens upstream of everything XP reads (this consistency is the
  core argument for swapping the evidence source instead of serving v2).
- No parser/Neo4j removal — the KG write path stays (learner model, Part 2).

### 3.7 Acceptance criteria

1. Flag OFF: existing apollo test suite green, no served-output diffs
   (byte-identical guarantee test, same pattern as clarification's).
2. Flag ON, replay fixtures: session-45 transcript ≥ 25%; attempt-47 transcript
   graded (non-abstained) with per-topic dock ≥ visible and final < C;
   controls remain F with 0 credited topics; every credited topic carries a
   validated span.
3. `grading_provenance` present in the response envelope in both flag states
   (OFF ⇒ `grader_used: llm_fallback`, `evidence_source: graph_nodes`).
4. ≥95% patch coverage (repo gate), no live-service calls in tests (LLM stubbed
   with recorded structured outputs).

## 4. Part 2 — additive v2 + misconception wiring (design, planned after Part 1)

Precedence rules (the whole part in four sentences):

1. **Graph can only add.** Per topic:
   `served_credit = max(llm_credit, graph_credit_if_resolved)` where graph
   credit requires resolver method confidence at/above its tier cap
   (`candidates.py:38-48` ladder). Provenance marks "confirmed by knowledge
   graph". Graph signals never subtract and never gate; abstention is removed
   from the serving decision entirely (the graph lane keeps computing it into
   pair artifacts for calibration).
2. **Misconceptions dock only through the judge gate.** Detector findings
   (judge-authoritative gate + F-struct co-key + banks + emergent capture — all
   merged, flag-OFF today) remain the sole dock source; graph contradiction /
   edge signals become *candidate evidence routed into the gate*, never direct
   docks.
3. **Docks are resolvable.** Wire `_finding_resolved` (today `return False`) to
   (a) clarification `confirmed` outcomes, (b) the adjudicator's
   `corrected_later`, (c) explicit later-turn correction. Resolved docks render
   struck-through at 0 points.
4. **Clarifications become grading-neutral and Done-driven.** `asked_waiting`
   never counts against the student (excluded from any unresolved accounting
   that reaches serving); probe text is persisted (fix `turn.py:81` by
   extracting the woven question from the drafted reply or generating the probe
   string first); probe *triggering* migrates from chat-time resolver residuals
   to Done-time adjudicator uncertainty (≤2 probes, else grade with what
   exists) — this piece is Part 2b and may ship separately.

## 5. Risks

| Risk | Mitigation |
|---|---|
| LLM over-credit / grade inflation (observed: transcript-audit saturation, 44% misc false-pass) | Verbatim-span validation; quantized credit; controls in the replay gate; misconception judge stays authoritative; calibration replay before every serving change |
| Prompt injection via student text into the judge | Spans must be verbatim quotes (can't be fabricated); structured output; student text framed as data; consistent with existing graders' exposure — flag for the standing security review |
| Judge variance run-to-run | Quantized credit levels; temperature 0; fixture snapshots asserted with tolerance at the topic-status level, not raw floats |
| Cost/latency at Done | One gpt-4o call replaces compute_coverage's batched LLM calls — net neutral or better; measure `grading_latency_ms` in artifacts (already recorded) |
| Two status vocabularies drift (`covered/partial/missing` vs `credited/misconception/unresolved`) | Part 2 adds the explicit adapter at the merge point; no silent mapping |

## 6. Sizing

- Part 1: ~1 focused week incl. tests/calibration. New code ≈ one module
  (~300–500 lines) + prompt + `done.py` swap + provenance plumbing + replay
  driver. Deletions/simplifications: served path no longer touches
  `coverage.py`'s silo machinery (586 lines stay for the graph lane only).
- Part 2: ~1 week. Merge rules + `_finding_resolved` wiring are small; the
  Done-time clarification retarget (2b) is the medium chunk.
