---
doc: apollo/conversation/handlers/done
description: apollo/handlers/done.py — handle_done, the grade-of-record orchestrator that assembles the whole Apollo grading path
owns:
  - apollo/handlers/done.py
related:
  - apollo/conversation/routing/errors
  - apollo/overseer/_index
  - apollo/overseer/transcript-coverage
  - apollo/overseer/rubric
  - apollo/overseer/topic-score
  - apollo/overseer/wrongness
  - apollo/overseer/diagnostic
  - apollo/overseer/grounding
  - apollo/overseer/aside-penalty
  - apollo/overseer/xp
  - apollo/conversation/handlers/grading-artifact-writer
  - apollo/conversation/hoot-bridge-reference-answer
  - apollo/projections/scorecard
  - apollo/projections/mastery
  - apollo/persistence/done-write-linkage
  - apollo/persistence/progress-repo
  - apollo/schemas/problem
last_verified: 2026-08-13
stub: false
---

# handlers/done — the grade-of-record ORCHESTRATOR

`handle_done` is `POST /apollo/sessions/{id}/done`. It **assembles the whole grade path** — the grading-path recipe (D21) starts here. Cross-cutting grading invariants (grading-lane, misconceptions-empty, composite-retired, score→letter→narrative) live in `overseer/_index`, not restated here.

## Interface

- `handle_done(*, db, neo, session_id, auto_done=False) -> dict` — the only public entry (called by `routing/router`, and by `handlers/chat` when the intent/questioning gate decides "done"; that gate passes `auto_done=True`). Returns the student grade payload (`rubric`, `topics`, `progress`, `scorecard`, `grading_provenance`, `transcript`, …).
- **M1 (P3.4) claim primitives** (wired into `handle_done`; see Invariants "Claim lifecycle"): `_CLAIM_PHASE`, `_STALE_CLAIM_AFTER`, `_claim_grading_slot`, `_release_grading_claim`, `_fence_grade_commit`, `_progress_block`, `_stored_grade_payload`.

## Data flow

Ordered grade assembly (each step delegates to the owner doc):

1. Load session + problem (`_find_problem`) + latest `ProblemAttempt`. **Empty-attempt guard** (2026-08-07, defect I1): zero persisted student messages (`_student_message_count`) → `EmptyAttemptError` (409 `empty_attempt`, `routing/errors`) BEFORE any mutation — no freeze/phase change/XP/narrative, attempt row untouched (marking it flips `is_reattempt_in_session` and docks XP on the real Done). An already-graded attempt short-circuits to its stored report; otherwise Done CASes the claim (Invariants "Claim lifecycle") then reads the graph (degraded Neo4j tolerated).
2. Derive the reference graph via `Problem.to_kg_graph` (`schemas/problem`).
2a. **Question ledger** (`_question_ledger`, P1.2b/P1.3): ONE read of this attempt's `QuestionOpportunity` rows (by `id`), feeding the adjudicator's `tally_context` (step 3 — `[{node_id, state, times_asked, student_quote|null}]`), the scorer's `asked_node_ids` (step 5) via `_probed_node_ids(rows)` — NOT the raw row set, since a degenerate `fallback_served` turn mints a row without probing anything — and the wrongness findings (step 3b). Any exception logs and yields `None` for all of them (pre-fix grade); an EMPTY ledger stays `frozenset()`.
3. **Transcript coverage** (the sole grader): `compute_transcript_coverage_with_spans` (`overseer/transcript-coverage`) over `_full_transcript` → coverage + validated evidence spans. `_course_evidence_safe` (before it) checks `INTERACTION2` + `INTERACTION_CONCEPTS`, then renders `grounding_bundle` into the optional `course_evidence` block (also step 6). `_full_transcript` excludes `TutoringMessage` tagged `intent == ASIDE_MESSAGE_INTENT_TAG` (`hoot-bridge-reference-answer`) — INTERACTION4 hint-lane text never enters grading, but the student's untagged triggering question does.
3a. **Hoot-assist cap** (INTERACTION5, `overseer/aside-penalty`): gated on `interaction5_enabled()` AND `interaction_allowed_for_concept`, `_aside_texts` fetches the same aside rows `_full_transcript` EXCLUDES (its exact complement) and passes them to the adjudicator as `hoot_asides`; `apply_aside_caps` (cap 0.5) then flat-caps every flagged node in the coverage BEFORE rubric / topic-score / diagnostic / artifacts, so all consumers see the same values.
3b. **Wrongness ladder** (P3.2, `overseer/wrongness`): `effective_wrongness_level` — the flag paired with the concept allowlist, INTERACTION5-style — is read ONCE. At level ≥1 the SAME ledger rows from 2a become `ledger_findings`, and `candidate_quotes` hands the adjudicator `wrongness_candidates` (graded nodes only, carrying the tally's verbatim quote) so it can CORROBORATE a finding it may never originate. Its answers ride back on `coverage["wrongness"]`.
4. `compute_rubric` (`overseer/rubric`) maps coverage into the axis rubric.
5. **Topic score** (`_compute_topic_score_safe` wrapping `compute_topic_score` / `compute_centrality`, `overseer/topic-score`): best-effort, computed always, and given `asked_node_ids` (step 2a) so never-engaged graded nodes leave the denominator (P1.2b). On success `served_rubric` REPLACES `overall` with the topic score/letter (new dict; `rubric` itself is never mutated).
5a. **S2′ and the ladder's consequences** (`_evaluate_wrongness`): evaluated AT DONE over the RAW result — pre-P3.1 the ledger carries no per-turn credit, and `would_ceiling` needs the raw score. At level ≥3 the scorer is re-run with `misconceptions=` + `ceiling_active=(level ≥ 4)` and that result SUPERSEDES the raw one, so exactly ONE `TopicScoreResult` reaches `served_rubric` / `topics[]` / narrative / artifact; a soft-failed re-run keeps the raw result rather than costing a grade.
6. `generate_diagnostic` (`overseer/diagnostic`) — grounded narrative plus, on topic-score JSON success, structured per-topic feedback from the student's verbatim utterances. Handed `graded_topics_only(topic_score)`, NOT the full result (an `unprobed` topic is excluded from the grade, so narrating it as a gap would contradict served `topics[]`, P2.1/U2); the remediation pass (`INTERACTION3`, ≤3 weak topics, citation-only) shares that view. Runs via `asyncio.to_thread` so the narrative LLM never blocks the event loop.
7. XP: `compute_xp_earned` / `compute_progress_envelope` / `apply_xp` (`overseer/xp` + `persistence/progress-repo`); reattempt detection via `has_prior_graded_attempt` (`persistence/done-write-linkage`). At level ≥3 `_wrongness_bonus_xp` adds decision-7's +10 per resolved node.
8. Persist canonical artifact `write_artifacts` (`grading-artifact-writer`) → then project mastery `_project_mastery` → `update_mastery_from_artifact` (`projections/mastery`), and render `render_scorecard` (`projections/scorecard`). From level 1 up, `_shadow_misconceptions` also hands `write_artifacts` the INTERNAL findings array as `shadow_misconceptions`, which lands in the persisted `grader_payload -> 'misconceptions'` column and NOWHERE else.

## Invariants & gotchas

- **The transcript adjudicator is THE only grading lane.** A `CoverageGradingError` propagates to the retryable 503 handler — never a fallback to an empty-graph or legacy grade. A degraded KG never yields a false F (grading reads the transcript).
- **`_compute_topic_score_safe` is soft-fail**: any exception → `topic_score=None`, `served_rubric is rubric` (byte-identical), and `topics` is absent (not null).
- **Structured feedback is additive and topic-only:** successful topic JSON serves as `student_response["feedback"]`; parse/LLM failure or no topic score leaves that key absent. `diagnostic_narrative` stays the string back-compat surface either way (flattened on success, legacy on soft-fail).
- **Remediation cannot affect grading:** one `try/except` encloses the whole copy-on-success pass; failure leaves feedback byte-identical (no `review` keys) and score/letter/narrative/XP/persistence unchanged. A non-null Interaction-1 bundle skips fresh retrieval.
- **Artifact write + artifact-derived mastery are own-failure-domain telemetry** — each owns its commit and swallows exceptions; neither voids the served grade. `_project_mastery` is skipped when `APOLLO_GRAPH_SIM_LAYER3_ENABLED` is on.
- **Course grounding never adds a failure mode** (`overseer/grounding`): `_course_evidence_safe` is soft-fail by construction (flag off / disallowed concept / corrupt bundle / ANY exception → `None`, byte-identical prompts); `grading_provenance["grounding"]` is the replay-diff hook.
- **The Hoot-assist cap owns its failure domain** (`overseer/aside-penalty`): the aside fetch and `apply_aside_caps` are wrapped so ANY exception logs and leaves `coverage` UNCAPPED. Additive `grading_provenance["aside_penalty"] = {enabled, cap: 0.5, assisted_node_ids}` when the gate fired; off → absent.
- **Wrongness is inert below its rung, and NEVER touches the rubric axis** (P3.2 §2.5). Level 0 skips the whole block — no candidates, no findings, no log. Level ≥1 corroborates and shadow-logs (`apollo_wrongness_observed` per EVIDENCE ENTRY, so one node can yield several rungs, plus one `apollo_wrongness_summary` counting entries and distinct nodes separately, and `ledger_entries` — G-L1c's over-fire DENOMINATOR, without which the `< 10% of ledger rows` gate cannot be computed from the shadow corpus); level ≥3 fills the containers at `dock_points 0.0` and awards the XP bonus; only level 4, which nothing sets, moves a number. **Fail-safe = miss** everywhere: an absent corroborator row, an absent credit, an ungraded node type or a soft-failed scorer yields no corroborated finding. The negative requirement: `_attempt_misconception_scores` and `compute_rubric(misconception_scores=…)` receive NOTHING new, because feeding them flips `AXIS_WEIGHTS["misconception_corrected"]` from absent to present and rescales every other axis — gated by `test_done_rubric_axis_inertness.py`.
- **Level 1 persists the findings even though it serves nothing** (spec §2.3 L1 "produce + persist + shadow-log"). L2c cross-attempt question memory is a level-**2** rung that reads what an EARLIER attempt wrote, through `prior_wrongness_findings` over `grader_payload -> 'misconceptions'`. Deriving that array only from `topics[].misconceptions` (level ≥3) would starve it: at level 2 every prior attempt wrote `[]`, so `controller._select_carried` has nothing to carry and L2c silently no-ops at the level it ships at. The SAME array is also what the decision-7 XP dedup subtracts, and there the corroborated set alone is not enough: S2′ requires NOT `corrected_later`, so a corroborated finding is never `resolved`, and an array of corroborated findings records nothing about the population the bonus pays — the "once per user × problem × node" guard degrades to "always empty" and +10 becomes re-earnable on every best-grade-wins retry. `_shadow_misconceptions` therefore persists BOTH populations (corroborated, plus `resolved AND apollo_elicited`), node-keyed with the corroborated rung winning the slot, at every level ≥1; `None` only at level 0, which keeps the row byte-identical. It is written to the DB column only — the returned payload, and so the student's scorecard *Watch out* list, still starts at level 3, and the persisted array is a superset that agrees with the served one entry for entry on the corroborated nodes.
- **The `shadow` marker is what keeps the TEACHER surfaces on rung 3** while that array starts at rung 1. `projections/classroom.top_misconceptions` and `projections/performance-insights`' `repeated_misconception` flag LATERAL over the SAME column with no level awareness, so persisting from level 1 would light them up two rungs early. Every entry written below `LEVEL_SURFACE` therefore carries `SHADOW_MISCONCEPTION_KEY` (`"shadow": true`) and both teacher readers exclude it; at level ≥3 the marker is absent, which is what turns them on. Gated on the WRITE side deliberately — one writer, so the two readers cannot drift, and the marker rides the free-form JSONB payload (no migration). The S9 read (`prior_wrongness_findings`) is deliberately marker-AGNOSTIC: its consumers ARE the level-1/2 population. Nothing else about the array moves with the level (`test_done_shadow_marker.py`).
- **The XP bonus population is `resolved AND apollo_elicited`**, never `corroborated` (S2′ requires NOT `corrected_later`, so that set is empty by construction), deduped against `prior_wrongness_findings` so it pays once per user × problem × node. Own failure domain, additive only — `apply_xp` raises on a negative delta and this can never produce one.
- The persisted `attempt.diagnostic_report` stores `{narrative, rubric (RAW), coverage, served_overall}` plus two conditional keys, absent when they don't apply: `auto_done: true` iff the questioning engine (not the student) triggered this Done (P0.4), and `unprobed_node_ids` — nodes P1.2b dropped from THIS grade, read by `projections/performance-problems` so its drill-down never re-derives a class-wide "missed" from `coverage` alone. `served_overall` snapshots `served_rubric["overall"]`; re-serving surfaces read it first, falling back to `rubric.overall` for pre-snapshot rows.
- The response keeps historical `graph_lane: null`; `done_turn_order` (the WU-4C1 shadow chain) is NOT imported — A7 removed it. **`grading_provenance.reference_question_asides_used`** (additive) reads `sess.metadata_[ASIDE_COUNT_SESSION_METADATA_KEY]`, default 0, teacher-only. `docks[]` / `score_before_dock` derive from `serialize_topics`; at level 3 `docks` becomes non-empty at `points: 0.0` — intended provenance ("we saw this, it cost nothing"), not a bug to special-case away.
- **Claim lifecycle (M1, P3.4; gated in `test_apollo_done_claim_postgres.py`):** `_claim_grading_slot` CASes `phase` (`IS DISTINCT FROM 'SOLVING' OR updated_at < now-15min`, NULL-safe) as Done's FIRST Postgres write — PHASE-ONLY (fix-round-2 reverted a stamp guard: `updated_at`'s model-level `onupdate` let ANY unrelated session write — chat's pending-intent commit, aside metadata, session_init — invalidate the RIGHTFUL owner's own stamp). **Integrity invariant**: the terminal fence `_fence_grade_commit` runs before `apply_xp` / every grade-visible write as one `UPDATE ... WHERE phase='SOLVING'`; Postgres serializes concurrent UPDATEs to a row, so AT MOST ONE Done ever passes it. Two ACCEPTED AVAILABILITY residuals (never integrity): a stale Done's release can reset a LIVE reclaim's phase (same value, no stamp to tell apart) — the reclaimer loses its fence and retries; a fenced-out/crashed Done's only recovery is `_STALE_CLAIM_AFTER`/`handle_retry`. Release runs under `asyncio.shield`; a SECOND `_stored_grade_payload` check post-claim catches a stale hoist and replays instead of re-grading.

## Env flags

- `APOLLO_GRAPH_SIM_LAYER3_ENABLED` (`_graph_sim_layer3_enabled`) — gates the mastery-projection interlock; default OFF everywhere.
- `INTERACTION2` (`config.settings.interaction2_enabled`) — gates course grounding of both prompts; default OFF, independent of `INTERACTION1` (which gates only whether the bundle is BUILT). Read ONLY here.
- `INTERACTION3` — weak-topic remediation citations; default OFF.
- `INTERACTION5` (`config.settings.interaction5_enabled`) — Hoot-assist grading cap; default OFF. Combined here with `interaction_allowed_for_concept`.
- `INTERACTION_CONCEPTS` (`config.settings.interaction_allowed_for_concept`) — optional concept-slug scope for course grounding, the Hoot-assist cap, and the wrongness ladder.
- `APOLLO_WRONGNESS_LEVEL` — the P3.2 ladder, default 0. Never read here directly: `overseer/wrongness.effective_wrongness_level` is the single reader, and `done.py` compares against the named `LEVEL_*` rungs rather than bare integers.

## Related

See `overseer/_index` for the grading-path cross-cutting invariants and the full directional chain: `transcript-coverage ↔ rubric ↔ topic-score ↔ wrongness ↔ done ↔ grading-artifact-writer ↔ scorecard ↔ mastery`. Aside tagging/cap: `hoot-bridge-reference-answer`.
