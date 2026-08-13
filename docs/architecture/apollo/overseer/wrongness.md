---
doc: apollo/overseer/wrongness
description: The pure P3.2 wrongness core — the single ladder-flag reader, ledger→findings, and the S2′ consequence predicate.
owns:
  - apollo/overseer/wrongness.py
related:
  - apollo/overseer/topic-score
  - apollo/overseer/transcript-coverage
  - apollo/conversation/handlers/done
  - apollo/persistence/done-write-linkage
  - platform/config-settings
last_verified: 2026-08-12
stub: false
---

# Overseer wrongness — S2′, the ladder, and the one flag reader

Apollo P3.2 (spec `2026-08-12-apollo-p32-wrongness-signal-design.md`). One module
owns *what a wrongness finding means and what it selects*, so no consumer
re-derives the predicate. Pure: no IO, no DB, no LLM, no side effects at import.

## Interface

- **`effective_wrongness_level(concept_slug) -> int`** — THE single reader of
  the ladder flag. `config.settings.wrongness_level()` clamped to `0..MAX_LEVEL`,
  forced to 0 unless `interaction_allowed_for_concept(concept_slug)` admits the
  problem's concept (the INTERACTION5 pairing, so the ladder can pilot on one
  concept). Every gate site calls this.
- **Rung constants** `LEVEL_OFF/PRODUCE/SCHEDULE/SURFACE/CEILING = 0..4`
  (+ `MAX_LEVEL`) so no gate site writes a bare `>= 2`.
- **`LedgerFinding`** — one evidence entry on one tally row, flattened:
  `node_id, wrongness, quote, contradicts, kind, turn_id, is_latest_evidence,
  state, times_asked, last_asked_turn`.
- **`WrongnessFinding`** — a finding after S2′: `node_id, quote, contradicts,
  kind, corroborated, resolved, apollo_elicited, would_ceiling`.
- **`ledger_findings(rows) -> tuple[LedgerFinding, ...]`** — duck-typed over
  `QuestionOpportunity`-shaped rows (`.reference_node_id`, `.state`,
  `.times_asked`, `.last_asked_turn`, `.evidence`).
- **`candidate_quotes(findings, *, graded_node_ids) -> dict[node_id, quote]`** —
  the corroborator's `wrongness_candidates` input.
- **`select_findings(*, findings, credits, second_reader, graded_node_ids,
  raw_score) -> tuple[WrongnessFinding, ...]`** — S2′ + the ladder.
- Constants `WRONGNESS_NONE/SELF/MATERIAL`, `WRONGNESS_VALUES`,
  `MIN_CORROBORATED_CREDIT = 0.6`, `CEILING_UNCORRECTED = 84`.

## Data flow

Producer → ledger → here → consumers:

1. `smart_questions/unified` labels each tally update (`wrongness` +
   `contradiction`); `smart_questions/controller` persists it as the tagged
   evidence entry `{turn_id, quote, wrongness, contradicts, kind}` on
   `app.question_opportunities.evidence` (free-form JSONB, **no migration**).
2. At Done, `handlers/done` reads the ledger ONCE, calls `ledger_findings`, then
   `candidate_quotes` to hand the at-Done adjudicator
   (`overseer/transcript_coverage`) the claims to corroborate.
3. The adjudicator returns `coverage["wrongness"][node_id] = {contradicted,
   corrected_later, prompted}`; `done` feeds that plus the topic credits and the
   raw score to `select_findings`, shadow-logs every finding, and — at level ≥3
   only — passes the corroborated ones to `overseer/topic_score` as
   `topics[].misconceptions` and to `grading/artifact_build`.
4. `smart_questions/controller` uses `ledger_findings` again at level ≥2 for
   probe priority, and `persistence/attempt_history.prior_wrongness_findings`
   for the one carried cross-attempt challenge.

## Invariants & gotchas

- **S2′ is the only consequence predicate.** `corroborated` iff ALL of:
  `wrongness == "contradicts_material"`; `credits[node_id] >= 0.6`; the entry is
  the node's MOST RECENT evidence; the second reader said
  `contradicted AND NOT corrected_later`; the node is graded. The rule it
  replaces ("final state `conflicting` ⇒ dock") scored **0/2** on the only two
  high-credit prod cases it fired on (attempt 86 = zero-transcript artifact,
  attempt 167 = a student self-correcting).
- **`conflicting` is a STICKY final state, not a live one.** Keying on state
  mistakes the tally's failure to relabel for the student's failure to revise —
  hence evidence recency + `corrected_later`.
- **Fail-safe = miss.** An absent second-reader row, an absent/unparseable
  credit, or a non-`bool` reader value never corroborates. The corroborator's
  silence can only ever remove a consequence, never create one (P0.5
  abstain-not-zero, carried).
- **Ungraded-type nodes are reported, never corroborated.** They feed the
  narrative and the teacher surfaces; grading them would silently widen the
  graded denominator, which is P1.4's decision.
- **`resolved` and `corroborated` are mutually exclusive by construction** (S2′
  requires `NOT corrected_later`). The decision-7 XP bonus population is
  therefore `resolved AND apollo_elicited` — a caller that asks for
  `corroborated AND resolved` gets the empty set, always.
- **`apollo_elicited` = `last_asked_turn is not None`** — the decision-7
  amendment, as a PRESENCE test. "Assert something wrong on a node Apollo never
  asked about, then fix it" is the farmable path, and it earns nothing. Spec §8
  D7 writes the guard as `last_asked_turn < correction_turn`, but no correction
  turn exists on the ledger (the second reader returns a `corrected_later`
  bool), and comparing against the CLAIM turn instead is self-defeating:
  `last_asked_turn` is a mutable row-level LAST value, so the ordinary L2a
  challenge loop (student errs → contested node probed → student fixes it)
  pushes it past the claim and turns the guard off in exactly the population
  the bonus exists to reward. The presence test is monotone and equals the
  spec's comparison wherever that comparison is reachable — a corrected node
  leaves `probeable_graded`, so Apollo cannot ask about it afterwards.
- **No double jeopardy.** INTERACTION5's flat 0.5 aside cap lands below
  `MIN_CORROBORATED_CREDIT`, so a capped node can never also carry a
  corroborated finding.
- **Import-light on purpose.** stdlib + `config.settings` only, asserted by
  `test_wrongness_predicate.test_wrongness_core_stays_import_light` (zero
  `apollo.*` imports). Importing `topic_score` for the one shared integer would
  not close a cycle *today*, but it would drag `apollo.ontology` and
  `overseer.rubric` in behind it and put the scorer on the questioning hot
  path's import graph once W2-A wires `controller → wrongness` — the chain the
  design is protecting is `unified → selection → topic_score`. So
  `CEILING_UNCORRECTED` is a duplicated constant pinned **unconditionally** equal
  to `topic_score`'s (the authority) by
  `test_wrongness_predicate.test_ceiling_constant_agrees_with_topic_score`.
- **Every function is total.** `evidence` is free-form JSONB (`__evidence__array_check`
  asserts only `jsonb_typeof = 'array'`); a malformed row is logged
  (`apollo_wrongness_ledger_row_skipped`) and skipped, never raised — this feeds
  the grade path and a Done must not 500 on a bad row.
- **`LedgerFinding.last_asked_turn` is additive to the frozen S6 shape** (it is
  the only input the Apollo-elicited guard needs, and it lives on the same row).
- **The env var has exactly one reader**, pinned by
  `test_wrongness_flag.test_no_other_module_reads_the_env_var`. A second reader
  would silently stop honoring the concept allowlist at that site.

## Level gating (S10 — the authority)

| Rung | Constant | What activates |
|---|---|---|
| 0 | `LEVEL_OFF` | nothing — byte-identical to the pre-feature build |
| 1 | `LEVEL_PRODUCE` | produce + persist + corroborate + shadow-log (incl. `would_ceiling`) |
| 2 | `LEVEL_SCHEDULE` | probe priority, the done-gate, the 1-per-attempt carried challenge |
| 3 | `LEVEL_SURFACE` | `topics[].misconceptions`, narrative line, teacher surfaces, XP bonus |
| 4 | `LEVEL_CEILING` | `min(raw, 84)` + `misconception_dock` — **built dark, nothing sets it** |

## Related

`apollo/overseer/topic-score` (owns `CEILING_UNCORRECTED` and the containers),
`apollo/overseer/transcript-coverage` (the corroborator),
`apollo/conversation/handlers/done` (the call site),
`apollo/persistence/done-write-linkage` (`prior_wrongness_findings`),
`platform/config-settings` (`wrongness_level`).
