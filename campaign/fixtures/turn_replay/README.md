# Turn-replay fixtures (Apollo P3.2 / P3.1 Phase 0)

Four committed, PII-scrubbed, **exact prod** attempts. They are the regression
corpus for the wrongness signal (`APOLLO_WRONGNESS_LEVEL`) and the input to
`campaign/turn_replay.py`. Nothing else in the P3.2 build re-derives them.

> **These are real student transcripts. They are fixture DATA and may never be
> pasted into a prompt.** The P1 rule "never quote real student text" governs
> *prompt exemplars* and is pinned by
> `apollo/overseer/tests/test_transcript_coverage_exemplars.py`. Prompt blocks
> (the WRONGNESS DUTY block, the corroboration instruction, the carried-challenge
> clause) use **paraphrased** exemplars only. Reading a fixture to build a prompt
> is a spec violation, not a shortcut.

Schema, PII rules, and the per-fixture role are enforced as an executable gate by
`campaign/tests/test_turn_replay_fixtures.py` — 41 assertions, no DB, no network.

---

## Provenance

| | |
|---|---|
| Source | prod Supabase (`uinkseewnxvumrxksnew`), export taken 2026-08-11 |
| Corpus | 259 attempts / 874 messages / 560 question-opportunity rows / 159 grading runs, 2026-07-26 → 2026-08-11, concepts 18 + 20 |
| Committed here | **4 attempts**, concept 20 (`ethics`) only |
| Curated | 2026-08-12, P3.2 build wave 0 |

### Why only four

The 217 shaped per-attempt bundles stay in the session scratchpad. Committing the
corpus would put a whole cohort's prose in git for the sake of four regressions.
Each fixture below earns its place by pinning a *named* defect; the cap is
asserted (`test_fixture_directory_holds_exactly_the_four_curated_attempts`).

### Each fixture's role

| File | `attempt_ref` | messages | ledger rows | evidence entries | verdicts | prod served |
|---|---|---|---|---|---|---|
| `attempt_083_paragraph_dump.json` | `prod-2026-08-11/attempt-083` | 2 (1 student) | 4 | 4 | 2 | 98 / A+ |
| `attempt_086_zero_transcript.json` | `prod-2026-08-11/attempt-086` | 0 (0 student) | 6 | 10 | 1 | 95 / A |
| `attempt_124_conflicting_graded.json` | `prod-2026-08-11/attempt-124` | 17 (8 student) | 4 | 7 | 2 | 60 / C |
| `attempt_167_self_correction.json` | `prod-2026-08-11/attempt-167` | 12 (6 student) | 6 | 14 | 1 | 90 / A |

- **083 — auto-done / unasked-credit exploit.** One 1,964-character polished
  student paragraph, Apollo replies "I have enough to grade what you taught me",
  every ledger row `times_asked == 0`, served **A+**. This is the G9 cohort shape
  the level-2 done-gate must *gate*. It measures **gating, not grade movement**
  (spec §4.1): replay cannot generate the student's answer to a question that was
  never asked, so no fixture here may be used to claim the gate "kills 83".
- **086 — zero-transcript I7 artifact.** Graded **A** with an empty transcript and
  a live 6-row ledger carrying evidence quotes for turns that no longer exist. It
  is the P0.1 empty-attempt-guard regression and the naive
  "final state `conflicting` ⇒ dock" rule's first false positive. **Kept empty on
  purpose** — do not "repair" it from the ledger quotes.
- **124 — contradiction that must not be corroborated on credit.** Graded node
  `q2_four_impairments` ends `conflicting` at credit **0.3**, below S2′'s
  `credit >= 0.6` bar. Protects the credit term of the predicate.
- **167 — self-correction protection.** Graded node `q19_map_roots` ends
  `conflicting` at credit **0.9** *after the student corrected himself in
  dialogue* ("So I guess I was wrong about governance since its more a mix of all
  of them..."). The sticky `conflicting` state is the tally's failure to relabel,
  not the student's failure to revise. S2′ must select **0** findings here in all
  four samples (gate G-FIX). This sentence is the regression; paraphrasing it
  destroys the fixture, so a test asserts the substring survives in **both** the
  transcript and the ledger evidence.

---

## Schema (FROZEN — `campaign/turn_replay.py` reads exactly this)

```json
{
  "fixture_version": 1,
  "attempt_ref": "prod-2026-08-11/attempt-167",
  "pii_scrubbed": true,
  "concept": {"id": 20, "slug": "ethics"},
  "problem": { "...full authored Problem payload, Problem.model_validate-able..." },
  "messages": [{"turn_index": 0, "role": "student|apollo", "content": "...", "intent": null}],
  "question_opportunities": [
    {"reference_node_id": "...", "state": "understood|tentative|missing|conflicting",
     "times_asked": 0, "last_asked_turn": null, "asked_turn": null,
     "answered_turn": null, "question": "",
     "evidence": [{"turn_id": 0, "quote": "..."}]}
  ],
  "adjudicator_output": {"verdicts": [
    {"node_id": "...", "covered": true, "credit": 0.9, "confidence": 0.86,
     "evidence_span": null, "prompted": false, "corrected_later": false,
     "contradicted": false, "basis": "stated", "hoot_assisted": false}
  ]},
  "recorded": {"served_score": 90, "served_letter": "A",
               "topic_credits": {"q19_map_roots": 0.9}}
}
```

Every key set above is asserted exactly (`test_fixture_schema_keys_frozen`). Notes:

- **`problem`** is the public `to_pydantic_payload` shape
  (`apollo/persistence/models.Problem.to_pydantic_payload`): `payload_extra`
  spread at top level, `id = problem_code`, `concept_id = <concept slug>`,
  `reference_solution = document["steps"]`. That mapping is what drops the DB row
  identities; it is not a fixture invention.
- **`evidence` entries carry exactly two keys** (`turn_id`, `quote`) — seam **S2**'s
  untagged shape, which `done._latest_student_quote` and `done._probed_node_ids`
  read today. A level-≥1 replay produces the *tagged* five-key shape at runtime;
  the fixture never pre-tags it.
- **`prompted` / `corrected_later` / `contradicted`** are present and `false` on
  every verdict, so a level-≥1 replay has a second-reader answer for every
  recorded node instead of an absent row (absent ⇒ fail-safe = miss, which would
  make the fixture silently untestable for corroboration).
- **`state`** is validated against the live
  `transcript_coverage._VALID_TALLY_STATES` — a divergence in any of the enum's
  four copies fails here too.

### `basis`: normalized, on purpose

The scratchpad bundles record `"basis": "replay-reconstructed"`, which is
**off-enum** for `coverage_contract.BASIS_VALUES`. `_to_coverage_verdict` drops an
off-enum basis with a `transcript_coverage_basis_off_enum` warning, so the replay's
`coverage["basis"]` map would come back empty and disagree with production for a
reason nobody would think to look for.

**Decision: normalized at curation time** — `"stated"` where `credit > 0`,
`"absent"` where `credit == 0`. `test_recorded_basis_is_on_enum_so_nothing_is_dropped`
pins both the enum membership and the substitution rule. The original value is not
recoverable from the fixture; it is recorded here and in the recipe below.

---

## PII scrub

### What was dropped

| Dropped | From |
|---|---|
| `user_id`, `course_id`, `session_id`, `learning_activity_id`, `attempt_id`, `problem_id` | the bundle `meta` block (removed entirely) |
| every DB surrogate `id`, `created_at`, `updated_at`, `quarantined_at`, `tier`, `solution_source`, `provenance` | the `problems` row |
| `metadata` (Hoot-aside citations: `storage_key`, `bucket`, `teacher_upload_id`, page assets) and `low_confidence_pattern` | every message |
| `student_declined` | every ledger row — all four fixtures record `false`, so the frozen schema's omission is lossless (curation **must fail loudly** if a `true` ever appears; see the recipe) |
| the `session` block (`phase`/`status`/`modality`) | not read by turn replay |

### What was deliberately kept

- **`concept.id = 20`.** A curriculum id, not a personal one, and it is what
  `wrongness.effective_wrongness_level(problem.concept_id)` gates on — a replay
  without it cannot exercise the concept allowlist. Frozen schema, deliberate
  exception to "drop every database id".
- **`attempt_ref`.** Carries the export date and the attempt ordinal as a
  provenance label only. It is never used as a key and resolves to nothing
  without prod access.
- **`problem.id`** = the authored `problem_code`
  (`authored.<md5-of-authored-content>`). A content hash of teacher-authored
  curriculum, not an identity.
- **Student prose, verbatim.** Non-negotiable: 167's self-correction sentence *is*
  the regression, and 083's essay quality is the whole point of §4.1's warning.

### The sweep, and the resolution of every hit

Run over **every decoded string** in each fixture, at every depth
(`test_no_pii_markers_in_any_fixture`):

| Sweep | Pattern | Hits | Resolution |
|---|---|---|---|
| email | `[\w.+-]+@[\w-]+\.[A-Za-z]{2,}` | 0 | — |
| URL | `https?://`, `www.` | 0 | — |
| `@handle` | `@[A-Za-z]\w{2,}` | 0 | — |
| phone-shaped | `\d{3}[-.\s]?\d{3}[-.\s]?\d{4}` | 0 | — |
| long digit run | `\d{7,}` | 0 | — |
| UUID | RFC-4122 shape | 0 | user UUIDs never entered the bundle (dropped with `meta`) |
| timestamp | `YYYY-MM-DDTHH:MM` | 0 | all real timestamps dropped |
| identity keys | `BANNED_KEYS` at any depth | 0 | `test_no_identity_or_timestamp_keys_survive` |

**Capitalized tokens — 86 distinct, all reviewed 2026-08-12, all retained:**

- **17 are person-name tokens**: `Bakici, Gregory, Harminder, Henfridsson, Jacob,
  Kagan, Mason, Ola, Richard, Robert, Shawn, Singh, Smith, Tyler, Wayne, Young,
  Zheng`. Every one is a **published academic author named in the assigned course
  reading** — Richard O. Mason (*Four Ethical Issues of the Information Age*, 1986)
  and the 2020 BIG PAPA authors (Jacob A. Young, Tyler J. Smith, Shawn H. Zheng),
  plus Robert Wayne Gregory / Ola Henfridsson / Kagan Bakici / Harminder Singh,
  whom the student in attempt 086 **incorrectly** credits with BIG PAPA. Retained:
  they are public bibliographic facts and the misattribution is graded content.
  **No student, teacher, or other private individual is named anywhere.**
- **69 are course terms or ordinary sentence-initial English** (`Privacy`,
  `Accuracy`, `Property`, `Accessibility`, `Governance`, `Module`, `Slides`,
  `Apollo`, `The`, `When`, …).

**17 name-shaped sequences** (2+ consecutive capitalized tokens or initials): the
7 author names above plus 10 course-term/phrase collisions (`Behavioral
Surveillance`, `Big Data`, `Information Age`, `When Mason`, …).

Both allowlists are **exhaustive** in the test, not illustrative: any future edit
that introduces a new proper noun fails the gate until a human reviews it. That is
the point — it is simultaneously a PII tripwire and a fixture-immutability
tripwire. Verified to bite on injected `sarah.jones@purdue.edu`, `https://…`,
`@handle`, `765-494-4600`, a 9-digit run, a UUID, an ISO timestamp, a
`metadata.user_id` key, and the name `Sarah Jones`.

The one string a scrub is most likely to "clean up" is protected by its own test:
`test_attempt_167_carries_the_self_correction_quote` fails the moment
`"I was wrong about governance"` is paraphrased.

---

## Regenerating

Not automated on purpose — regeneration needs prod export access and a human PII
review, so a committed script would imply otherwise. The recipe:

1. Export `app.problem_attempts`, `app.tutoring_messages`,
   `app.question_opportunities`, `internal.grading_runs` for the window, plus the
   `app.problems` rows they reference.
2. Shape one bundle per attempt (`{meta, concept, problem, session, messages,
   question_opportunities, adjudicator_output}`). Verdicts are reconstructed from
   the latest canonical grading run: `covered ← node_ledger[key].status ==
   "credited"`, `credit ← score_details.topic_score.topics[].credit`,
   `confidence ← node_ledger[key].confidence` (default 1.0),
   `evidence_span ← topics[].evidence_span`, `hoot_assisted ←
   topics[].hoot_assisted`.
3. Project each chosen bundle onto the frozen schema above: `problem` via
   `to_pydantic_payload`, messages down to the 4 keys, ledger rows down to the 8
   keys, verdicts + `contradicted:false` and the `basis` normalization, `recorded`
   from the attempt's served score/letter and stored topic credits. Abort on any
   ledger row with `student_declined: true` — the frozen schema has nowhere to put
   it, and silently dropping a real decline would falsify the fixture.
4. Re-run the PII review by hand, update the allowlists and the tables above, and
   run `pytest campaign/tests/test_turn_replay_fixtures.py -q`.

Fixtures are **frozen data**. Changing one is a reviewed act: the schema test, the
allowlist tests, and the per-fixture role tests will all object first.
