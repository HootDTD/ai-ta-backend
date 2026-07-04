# O2 — /chat 422 attrition triage (lane B-stretch/O2)

Repo: ai-ta-backend @ staging (2c2dc5f). Pure diagnosis, read-only. Local stack UP (:8000).

## TL;DR

- The chat-route 422s are **`parser_could_not_extract`**, NOT `malformed_equation`. The
  O2 finding's "malformed_equation" label is a **misattribution** — `malformed_equation`
  is raised only by the SymPy solver (`apollo/solver/sympy_exec.py`) on the **Done** path,
  which is never reached from `/chat`. On the default `/chat` config the *only* reachable
  422 is `parser_could_not_extract`.
- **Root cause (product side, arguably WAI):** when a `vague_then_clarifies` persona sends a
  long, on-topic-but-content-empty utterance (word-salad), the parser extracts **zero nodes**
  from a message that `_is_non_trivial()` judges non-trivial → `ParserCouldNotExtractError`
  → HTTP 422. `apollo/parser/parser_llm.py:379-380`.
- **Root cause of the *attrition* (harness side):** the campaign driver calls
  `resp.raise_for_status()` inside the clarification loop and its outer `try/except` records
  the entire attempt as `status="error"`, discarding all the good teaching turns.
  `campaign/cast/student.py:509` + `:389`. **This is the 8% attrition and it is a harness
  artifact.** Campaign is frozen this weekend → **Monday item.**
- Confidence: **High** for the code path and trigger (4/4 transcripts match; deterministic
  code trace). Reproduction not run live (see §5) — the trigger is inherently LLM-draw
  dependent, which also explains the non-determinism the campaign diagnosis noted.

## 1. Per-incident inventory

All errored attempts across the two committed files + the untracked b0 rerun. The 422s are
this lane (O2). The 500s on `/done` are a *different* class (out of O2 scope — likely O1).

| Run | Line | Persona | Subject | Problem | Route | Status | Error |
|-----|------|---------|---------|---------|-------|--------|-------|
| f1   | 14 | vague_then_clarifies | fluid_mechanics | bernoulli_horizontal_pipe_find_p2 | `/chat` (sess 20) | **422** | parser (see below) |
| f1c  | 12 | vague_then_clarifies | fluid_mechanics | bernoulli_full_find_p2 | `/chat` (sess 13) | **422** | parser |
| f1c  | 15 | vague_then_clarifies | fluid_mechanics | continuity_area_change_find_v2 | `/chat` (sess 16) | **422** | parser |
| f1c  | 29 | vague_then_clarifies | macroeconomics | gdp_identity | `/chat` (sess 30) | **422** | parser |
| f1   | 0  | misconception | fluid_mechanics | bernoulli_full_find_p2 | `/done` (sess 6) | 500 | NOT O2 (done path) |
| f1c  | 34 | misconception | linear_motion | cyclist_accel_v_and_distance | `/done` (sess 35) | 500 | NOT O2 |
| f1c  | 35 | partial | linear_motion | cyclist_accel_v_and_distance | `/done` (sess 36) | 500 | NOT O2 |
| b0smoke | — | — | fluid (chunk) | — | — | 16/16 ok | 0 errors this draw |

**4 of 4 chat-route 422s are `vague_then_clarifies`.** Perfect correlation with the vague
persona archetype. (The raw JSONL records `error` as an httpx `HTTPStatusError` repr with only
the status line — the driver discards the JSON error body, so `error_code` is not in the record.
The code is unambiguous from the route + the trace below.)

### Trigger content (the last student POST that returned 422)

The transcript ends on a **student** turn in every case — that message is the one that 422'd
(no apollo reply was ever produced). All four are long and on-topic but assert nothing concrete:

- **f1 L14** (349 ch): *"Got it, when Apollo asks for clarification, I should respond with a
  correct and clear explanation to resolve his confusion. If there are any further questions
  about the principles or calculations involved in finding the pressure at section 2, I'm ready
  to provide an accurate and straightforward answer…"* — persona has **broken character** and is
  narrating its own instructions (meta-talk). Zero teachable content.
- **f1c L12** (735 ch): *"…something interesting happens with the flow… something related to the
  rate at which the water moves… it affects a particular characteristic… but I'll leave it at
  that."* — deliberate vagueness, no equation, no named quantity.
- **f1c L15** (499 ch): *"…there's a certain approach involving numbers and operations that just
  might unlock the solution… certain crucial values are substituted into the relevant… principle
  involving aspects like size and speed, pointing us directly to… something key."* — word-salad.
- **f1c L29** (628 ch): *"…there's this idea that you bring in, kind of balance against these
  expenditures to… capture an overall picture, maybe?… a sort of combined dance…"* — word-salad.

## 2. Traced 422 code paths on `/chat`

Route: `apollo/api.py:143` `chat()` → `handle_chat()` (`apollo/handlers/chat.py:241`) →
`parse_utterance(message, …)` (`apollo/handlers/chat.py:311`).

`parse_utterance` (`apollo/parser/parser_llm.py:347`) raises in two spots:
```
368  try: payload = json.loads(raw)
370  except json.JSONDecodeError:
371      if _is_non_trivial(utterance, concept): raise ParserCouldNotExtractError(utterance)
...
376  nodes, _, index_to_node = _build_nodes(raw_entries, …)
379  if not nodes and _is_non_trivial(utterance, concept):
380      raise ParserCouldNotExtractError(utterance=utterance)   # <-- the 422
```
`ParserCouldNotExtractError` → handler `parser_could_not_extract_handler`
(`apollo/api.py:335`) → **HTTP 422**, `error_code:"parser_could_not_extract"`, `utterance=<msg>`.

`_is_non_trivial` (`apollo/parser/parser_llm.py:124`) decides whether an empty parse should
raise:
1. `< 10` chars → trivial (no raise).
2. exact match in `_TRIVIAL_ACKS` → trivial.
3. `_EQUATION_LIKE` matches → **non-trivial** (short-circuit, no LLM).
   `_EQUATION_LIKE = re.compile(r"[=*/^+]|\d+\.?\d*|\^|\*\*")` — **matches any digit.**
4. else → LLM teaching-classifier; non-trivial iff `is_teaching and conf ≥ threshold`.

Verified against the four triggers:
- **f1 L14** short-circuits via rule 3 — the substring **"section 2"** supplies a digit →
  `_EQUATION_LIKE` matches → non-trivial=True with no LLM call. Zero nodes → raise.
- **f1c L12/L15/L29** have no math char → fall to rule 4; the LLM classifier returns
  `teaching=True` (they are on-topic prose about continuity / Bernoulli / the GDP identity),
  above threshold → non-trivial=True. Zero extractable nodes → raise.

### Why NOT `malformed_equation` / `filter_rejected`

- `malformed_equation` is raised only in `apollo/solver/sympy_exec.py:55,66`, invoked from the
  **Done** grading path. `grep` confirms the solver is **not imported on the chat path**
  (`apollo/handlers/chat.py`, `apollo/parser/parser_llm.py`). It cannot fire on `/chat`.
- `filter_rejected` is raised only in `apollo/agent/output_filter.py`, reached from
  `guard_clarification_reply`, which is gated behind `APOLLO_CLARIFICATION_ENABLED`
  (**default OFF; unset in the running stack**). The v1 chat path has no output filter by design
  (`chat.py:340-343`). It cannot fire in the campaign config.

⇒ On the default `/chat` path the only reachable 422 is `parser_could_not_extract`.

## 3. Harness path — why one 422 kills the whole attempt

The vague persona provokes clarifying questions from Apollo, and the driver's `?`-heuristic
keeps the loop alive until a turn 422s:

- `_play_clarification_followups` (`campaign/cast/student.py:241`) loops
  `while turns < clarification_max_turns` **as long as the last apollo reply contains `"?"`**
  (line 254). Apollo, unable to extract anything, keeps asking clarifying questions (all contain
  `?`) — including its off-task/derail nudges seen at f1 L14 turns 15 & 17 — so the loop keeps
  pulling fresh persona messages that degenerate further into content-empty word-salad.
- Each iteration calls `client.chat()` → `resp.raise_for_status()` (`student.py:509`). A 422
  raises `httpx.HTTPStatusError`.
- That propagates out of `_play_clarification_followups` into `run_attempt`'s broad
  `except Exception` (`student.py:389`), which records `status="error"`,
  `error=repr(exc)[:500]`, and **returns before `/done`** — the entire attempt (all prior good
  teaching turns) is discarded. This is the attrition.

Non-determinism explained: firing requires (a) Apollo keeping a `?` in its reply AND (b) the
persona's *particular LLM draw* producing an on-topic message that parses to zero nodes. The
b0smoke rerun drew non-degenerate messages → 0 errors, exactly as the campaign diagnosis noted.

## 4. Root cause + confidence

- **Attrition root cause (owner: HARNESS):** `run_attempt` treats a mid-loop
  `parser_could_not_extract` 422 as a fatal attempt error instead of a benign
  "Apollo couldn't parse that vague turn". **Confidence: High.**
- **Underlying 422 (owner: BACKEND / product decision):** a non-trivial-but-content-empty
  student utterance returns a hard 422 with no tutor reply. This is *intentional* current
  behavior (`parse_utterance` docstring says it raises on zero-extraction non-trivial input),
  so it is arguably WAI — but it is a rough UX edge: a real rambling student gets their message
  rejected instead of a "can you be more concrete?" nudge. **Confidence: High that this is the
  code behavior; the question of whether it *should* 422 is a product call, not a clear bug.**

## 5. Reproduction status

Not executed live. Rationale:
- The static trace is deterministic and matches 4/4 transcripts; the digit/LLM short-circuit
  behavior of `_is_non_trivial` was verified against the actual trigger strings.
- The trigger's LLM half (persona word-salad that parses to zero nodes) is inherently
  draw-dependent — a single hand-crafted live call may or may not fire, so a green live call
  would not falsify the finding.
- A faithful end-to-end repro needs a minted Supabase-local student token + provisioned session
  (`mint_student_token` hits the Supabase admin API); `OPENAI_API_KEY` is also not exported in
  this diagnosis shell (only in the server process). Minting touches Supabase, which is out of
  scope for read-only diagnosis. A safe deterministic repro *is* possible as a Monday unit test:
  feed f1c L15's text to `parse_utterance` with a stubbed `_call_extraction` returning
  `{"entries":[]}` and assert `ParserCouldNotExtractError` (drives `_is_non_trivial` rule 4 via
  the real classifier, or force rule 3 with any digit).

## 6. Recommendations

**Monday, harness (primary — fixes the 8% attrition), owner: campaign:**
In `campaign/cast/student.py`, catch `HTTPStatusError` with `status_code == 422` /
`error_code == "parser_could_not_extract"` inside `_play_clarification_followups` (and
`_play_scripted_beats`). Treat it as a benign "Apollo couldn't parse this vague turn":
break the clarification loop and proceed to `/done`, or record a distinct non-fatal outcome
(e.g. `status="ok"` with a `parse_gap` note) — do **not** discard the whole attempt. This
removes the attrition without any product change and keeps the vague persona meaningful.

**Optional, backend (product-owner call, NOT a required fix):**
Consider degrading `parser_could_not_extract` on the *teaching* path from a hard 422 to a
graceful clarifying reply ("I'm not quite following — can you state that more concretely?")
so real rambling students aren't hard-rejected. This is a design decision (the current 422 is
intentional and the FE has a handler for it), so it should not be slipped in silently; raise it
with the Apollo product owner rather than treating it as a bug.

**Docs nit:** the O2 finding's "malformed_equation" label should be corrected to
`parser_could_not_extract` in the campaign diagnosis writeup — they are different errors on
different routes.
