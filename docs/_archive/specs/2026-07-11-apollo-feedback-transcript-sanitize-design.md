# Apollo grade feedback: attempt transcript + narrative sanitization — design

- **Date:** 2026-07-11
- **Status:** approved (user-approved design, this doc is the record)
- **Repos:** ai-ta-backend (primary), ai-ta-student-ui (consumer)
- **Branches:** `feat/apollo-feedback-transcript-sanitize` off `staging` in both repos; two PRs into `staging`

## Problem

Two defects observed live on staging (MGMT 38200 course, attempt 62):

1. **Narrative leaks scoring internals to students.** The served diagnostic narrative
   contained fragments like `(proc_explain_causality, credit 0.90, weight 0.23)` and
   `(misconception dock: 0.000)`. Root cause is not LLM drift: the prompt builder
   (`apollo/overseer/topic_narrative.py::_format_topic_line`) feeds the model ledger lines
   containing canonical keys, `credit=`/`weight=` decimals, and dock values, while the
   system prompt orders it to "narrate ONLY the ledger". No sanitization exists anywhere
   downstream.
2. **Students cannot review the conversation that produced the grade.** The report view
   (`ApolloReportPanel.tsx`) has no access to the chat: `ApolloChat`'s messages are local
   component state destroyed on unmount, and `handleDone` never refetches. No endpoint
   returns attempt-scoped messages (`GET /sessions/{id}` returns only the *current*
   attempt's messages — stale/wrong after retry/next).

## Decisions (user-confirmed)

- Transcript dropdown shows **only the graded attempt's turns**, not the whole session.
- Narrative may echo **percentages** that the topic list already displays; it must never
  contain canonical keys, weights, decimal credits, docks, or scoring mechanics.
- Delivery mechanism: transcript is **included in the Done response** (no second fetch,
  no staleness); sanitization is **prompt fix + deterministic output gate** (belt and
  suspenders).

## Design

### 1. Backend — transcript in the Done response

In `handle_done` (`apollo/handlers/done.py`), after grading completes, read the graded
attempt's `Message` rows (`WHERE attempt_id = <graded attempt> ORDER BY turn_index` —
same query shape as `handlers/lifecycle.py`) and attach:

```
student_response["transcript"] = [{"role": str, "content": str, "turn_index": int}, ...]
```

- Fetch is wrapped: on DB failure, log server-side and set `[]` — a transcript failure
  must never cost the student their grade.
- No new endpoint, no new auth surface (`POST /sessions/{id}/done` is already
  owner-gated via `require_session_owner`).

### 2. Backend — narrative prompt fix + output gate

**Prompt (root cause), `apollo/overseer/topic_narrative.py`:**

- `_format_topic_line` emits `Topic "<display_name>": <status> — <credit*100>%` — no
  backticked canonical key, no `credit=`/`weight=` decimals. Fallback when
  `display_name` is missing: humanized key (underscores → spaces, prefix stripped),
  never the raw key.
- Misconception sublines keep the verbatim evidence span and qualitative
  resolved/uncorrected status; dock decimals removed.
- `build_topic_narrative_prompt`: `Coverage component` and `Misconception dock` lines
  removed from the user message; `Score: <n> (<letter>)` stays.
- System prompt gains an explicit rule: never mention internal identifiers, weights,
  decimal credits, docks, or scoring mechanics; percentages matching the topic list are
  allowed.

**Gate (backstop), same module:**

- `sanitize_narrative(text: str, canonical_keys: Sequence[str]) -> str` — pure function,
  returns a new string. Strips exact canonical keys (word-boundary match), removes
  parenthetical fragments matching `credit=`/`weight=`/`dock` patterns, collapses
  leftover empty parens/double spaces. Idempotent. Preserves `$...$` math.
- Applied in `generate_diagnostic` (`apollo/overseer/diagnostic.py`) as the last step
  before return — covers the topic path, the legacy non-topic path, and the
  deterministic append helpers.
- The dormant `apollo/grading/diagnostic.py::generate_constrained_diagnostic` path
  (graph-sim promotion, flags OFF) is intentionally untouched.

### 3. Student UI — dropdown + type sync

- `lib/apollo/api.ts`: add `TranscriptTurn = {role, content, turn_index}` and
  `transcript?: TranscriptTurn[]` on `DoneResponse`.
- `ApolloReportPanel.tsx`: new **collapsed-by-default**
  `<details><summary>Your conversation with Apollo</summary>` section — same idiom as
  the existing narrative `<details open>` block — placed between the diagnostic
  narrative and the action buttons. Each turn renders a role label (You / Apollo) and
  content through `MathMarkdown`.
- If `transcript` is absent or empty (old payloads, fetch failure), the section does
  not render.

### 4. Error handling

| Failure | Behavior |
|---|---|
| Transcript DB read fails | logged, `transcript: []`, grade unaffected |
| Narrative LLM fails | existing placeholder string; sanitizer no-ops |
| UI receives no transcript | section omitted; rest of report identical |
| Sanitizer edge cases | deterministic + pure; worst case returns input unchanged |

### 5. Testing (95% patch gate, backend)

- Prompt-builder tests: built prompt contains **no** canonical keys and no
  `weight`/`credit=` fragments; percentages present.
- Sanitizer fixtures: leaky narrative → clean; idempotency; `$...$` math preserved;
  empty-keys case (legacy path).
- `handle_done` test: mocked leaky LLM response → served narrative clean; `transcript`
  present, ordered by `turn_index`, scoped to the graded attempt.
- Transcript-failure test: DB error on message read → grade still returned,
  `transcript: []`.
- UI: no test runner wired — untested changes listed explicitly in the PR description
  per the workspace test-coverage contract.

## Doc reconciliation (drift contract, same commits)

- `ai-ta-backend/docs/architecture/apollo.md` (owns `apollo/**`): Done payload shape
  (`transcript` field), narrative prompt + sanitizer behavior.
- `ai-ta-student-ui/docs/architecture/components.md` (owns `components/**`):
  `ApolloReportPanel` transcript section.
- `ai-ta-student-ui/docs/architecture/_overview.md` (owns `lib/**`): `DoneResponse` /
  `TranscriptTurn` types.

## Evidence / provenance

- Leaky narrative observed on staging attempt 62 (search_space 5, problem
  `scrape.14.q740c916e55f5b0ff256e60ecc06c7988`); artifact rows in
  `apollo_grading_artifacts` ids 43/44 show the ledger fields that leak.
- Exploration report (code paths, line numbers): session 2026-07-11, Explore agent
  run — key facts embedded above.
