# Hoot aside: tutor-voice refresher + grading cap for Hoot-assisted topics

Date: 2026-07-28. Follows PR #203 (INTERACTION1-4). Branch:
`feat/apollo-aside-tutor-grade-cap` off `staging`.

## Goal

Two features, in order:

1. **Aside answer redesign** — the INTERACTION4 "Ask Hoot" aside currently
   reuses Hoot's standalone chat `TUTOR_PROMPT` verbatim (`## Answer` headings,
   `## Key Takeaway` / `## Check Your Understanding` sections, CYU-response
   rules, per-question-type length rules). Replace it with a **compact
   tutor refresher** purpose-built for a mid-teaching-session lookup.
2. **Grading cap for Hoot-assisted topics** — topics whose content a Hoot
   aside explained are credit-capped in grading: the student didn't fully
   teach them. **Flat cap, no earn-back** (user decision 2026-07-28).

## Decisions (user-confirmed)

- Aside shape: **compact refresher** — ~60–150 words, plain prose, keeps
  LaTeX + inline citations, NO headings, NO Check-Your-Understanding, NO
  Key Takeaway, ends by handing the student back to teaching.
- Penalty rule: **flat cap, no earn-back** — any topic a Hoot aside
  substantively explained is capped for this attempt even if the student
  re-teaches it well afterwards.
- Cap size: **credit = min(earned, 0.5); status never `covered`**
  (assisted + credit > 0 → `partial`; assisted + credit == 0 → `missing`).

## Feature 1 — compact refresher prompt (rides INTERACTION4)

**New module** `ai/prompts/apollo_aside.py` exporting `apollo_aside_prompt()`.
The prompt MUST:

- Keep, near-verbatim from `ai/prompts/tutor.py`: source-boundedness (CORE
  OPERATING RULE + NON-NEGOTIABLE 1–3), citation discipline (exact marker
  format, claim-level citations), scope control, the strict no-outside-facts /
  no-numeric-fabrication rules, LaTeX formatting rules, the relevance-check
  JSON contract (`not_relevant` flag), and the "'steps' field MUST be a
  single Markdown-formatted string" JSON output contract — the solver's
  response parsing (`_build_solution_from_data`) is unchanged.
- Replace everything structural: the reader is a student who is mid-way
  through **teaching this concept to a confused AI learner** and clicked a
  "look it up" button. Answer as a tutor giving a quick, confident refresher
  on exactly what was asked: plain prose (no `## Answer` heading, no lists
  unless the content is genuinely enumerable, no em-dash label lines),
  60–150 words, direct answer first, citations attached to claims, display
  math only when it materially helps. NEVER include Check Your Understanding,
  Key Takeaway, practice questions, or follow-up questions — the AI learner
  (Apollo) asks the questions in this product, not Hoot. End with ONE short
  sentence handing the student back to teaching, varying naturally around
  "now try putting that in your own words" (this sentence is part of the
  prose, not a heading; no citation needed on it).
- Keep the multipart/mixed-relevance handling and out-of-scope sentence
  contract from the tutor prompt (the `_OUT_OF_SCOPE_TEXT` / `_NOT_FOUND_TEXT`
  fallbacks in `reference_answer.py` are unchanged).

**Plumbing:** add keyword-only `system_prompt_override: str | None = None` to
`_prepare_solve_prompt` and `solve_with_bundle` in `ai/main_ai.py` (threaded
through; `None` → byte-identical prompt to today — test this).
`apollo/hoot_bridge/reference_answer.py` passes
`system_prompt_override=apollo_aside_prompt()`. `format_answer` call
unchanged. No change to `solve_with_bundle_stream` (Hoot chat path untouched).

## Feature 2 — Hoot-assist grading cap (new flag INTERACTION5)

**Flag:** `INTERACTION5` env (default OFF), same truthy parsing + call-site
`interaction_allowed_for_concept(problem.concept_id)` allowlist as
INTERACTION1-4 (`config/settings.py`). OFF → grading byte-identical.

**Data flow (all in the Done pipeline, `apollo/handlers/done.py`):**

1. New helper `_aside_texts(db, attempt_id)` → ordered tuple of
   `TutoringMessage.content` where `intent == ASIDE_MESSAGE_INTENT_TAG`
   (role `apollo`). Empty tuple when none. The existing `_full_transcript`
   exclusion is UNCHANGED.
2. `compute_transcript_coverage_with_spans(...)` (and the underlying
   adjudication in `apollo/overseer/transcript_coverage.py`) gains
   keyword-only `hoot_asides: tuple[str, ...] = ()`. When non-empty, the
   user payload gets a labeled block: `HOOT LOOKUP ANSWERS` — "Hoot answered
   these reference questions FOR the student during the session. This is NOT
   the student's teaching. Never use this text as evidence the student
   understands anything; `evidence_span` must still quote the STUDENT only."
   (mirror the tone of `_COURSE_EVIDENCE_INSTRUCTION`). When empty, prompt
   and schema are **byte-identical** to today (test).
3. Verdict schema gains `hoot_assisted: {"type": "boolean"}` ONLY when
   asides are present (strict schema: keep `required` consistent with
   `properties` for whichever variant is built). Grader instruction:
   `hoot_assisted=true` iff a HOOT LOOKUP ANSWER substantively explains this
   rubric node's content — independent of what the student later said (flat
   cap, no earn-back). `NodeVerdict` gains `hoot_assisted: bool = False`;
   the coverage dict rows carry it through.
4. New pure function (new module `apollo/overseer/aside_penalty.py`):
   `apply_aside_caps(coverage: dict, *, cap: float = 0.5) -> dict` — returns
   a NEW coverage dict (no mutation) where every node with
   `hoot_assisted=true` has `credit = min(credit, cap)` and its coverage
   status is downgraded so it can never grade `covered` (match the actual
   coverage-dict shape — read `_credit_for_node` in
   `apollo/overseer/topic_score.py` and `compute_rubric` first; the cap must
   flow into BOTH identically). Also returns/exposes the assisted node ids.
5. `done.py` applies step 4 immediately after coverage computation and
   BEFORE `compute_rubric`, `compute_topic_score`, and `generate_diagnostic`,
   gated on `interaction5_enabled() and interaction_allowed_for_concept(...)`
   and `hoot_asides` non-empty — so rubric, topic score, narrative, and
   artifacts all see the SAME capped values. Flag OFF or no asides → the
   original coverage object is used untouched.
6. **Provenance (additive):** `grading_provenance["aside_penalty"] =
   {"enabled": bool, "cap": 0.5, "assisted_node_ids": [...]}`.
7. **Feedback surfacing:** `TopicScoreResult` topic entries and the
   structured `topic_feedback` gain additive `hoot_assisted: bool`; the
   diagnostic/narrative prompt (only when assisted topics exist) may state
   that Hoot covered that topic for the student ("You asked Hoot about X —
   next time try teaching it yourself"). Absent asides → prompts
   byte-identical (test).

**Failure domain:** the aside-fetch + cap pass follows the INTERACTION3
pattern — any exception is logged and swallowed, grading proceeds uncapped.
It must never touch the `CoverageGradingError → 503` contract.

## Non-goals

- No DB migration (no new columns; provenance/feedback are JSONB-additive).
- No student-UI changes this session (aside card renders prose fine;
  `hoot_assisted` in feedback is additive — UI follow-up optional).
- No change to INTERACTION1-3 behavior, the aside cap count (3), the
  semantic filter, or the solution-doc exclusion.

## Testing (95% diff-cover vs origin/staging — CLAUDE.md gate)

- Byte-identical-when-off tests: `system_prompt_override=None`;
  `hoot_asides=()`; `INTERACTION5` unset → identical prompt bytes / schema /
  scores (mirror `test_grounded_prompts.py` style).
- Aside prompt: no CYU/KT/headings in prompt contract; compose test that the
  override reaches the LLM call (mock, à la `test_reference_answer.py`).
- `apply_aside_caps`: pure-function tests — cap math, covered→partial,
  zero-credit→missing, no-mutation of input, empty/absent flag passthrough.
- Adjudicator: schema variant with/without asides validates; `hoot_assisted`
  parsed into verdicts; instruction block present only with asides.
- `done.py` E2E handler test (existing `_done_fixtures` style): asides in DB +
  flag ON → capped topic score, provenance block, feedback flag; flag OFF →
  byte-identical grade.
- Full suite + `diff-cover --compare-branch=origin/staging --fail-under=95`.

## Docs (drift contract — same commit as code)

Owner leaves to reconcile + `last_verified` bump: 
`docs/architecture/apollo/conversation/hoot-bridge-reference-answer.md`,
`docs/architecture/apollo/overseer/transcript-coverage.md`, the topic-score
and done-handler leaves, `docs/architecture/platform/config-settings.md`,
the `ai/` prompts / main-ai owner leaf (find via `docs/index.json`), new
leaf or `owns:` additions for `ai/prompts/apollo_aside.py` and
`apollo/overseer/aside_penalty.py` (bijection lint must pass:
`docs` CI job / local lint script).

## Agent plan (5 Opus agents, 3 waves, one worktree, agents do NOT commit)

- **Wave 1 (parallel):**
  - **A — aside prompt:** `ai/prompts/apollo_aside.py` (new),
    `ai/main_ai.py` (override param), `apollo/hoot_bridge/reference_answer.py`,
    tests in `apollo/hoot_bridge/tests/` + `tests/` for main_ai override.
  - **B — adjudicator:** `apollo/overseer/transcript_coverage.py`,
    `apollo/overseer/coverage_contract.py` (if verdict validation lives
    there), tests in `apollo/overseer/tests/`.
- **Wave 2 (sequential after wave 1):**
  - **C — cap + wiring:** `apollo/overseer/aside_penalty.py` (new),
    `apollo/overseer/topic_score.py` (TopicScoreResult flag),
    `config/settings.py` (INTERACTION5), `apollo/handlers/done.py`
    (fetch asides, thread through, apply caps, provenance), handler tests.
  - **D — feedback/narrative:** `apollo/overseer/diagnostic.py` /
    `topic_narrative.py` surfacing, tests; touches `done.py` only if C left
    a clean seam.
- **Wave 3:** **E — integrator:** docs reconciliation + bijection lint,
  full pytest + diff-cover gate, conventional commits.
