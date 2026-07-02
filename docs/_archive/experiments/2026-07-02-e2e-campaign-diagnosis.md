# E2E Campaign Diagnosis — What's Missing Before the Graph Path Becomes the Main System

**Date:** 2026-07-02 · **Author:** Claude (Fable), overnight autonomous campaign, adjudication by Fable
**Instrument:** the e2e campaign harness (branch `feat/apollo-e2e-campaign-harness`): canonical grading artifact + projections (Phases A–B), local Docker stack (Supabase 57320-29 + Neo4j 57687), 38-persona corpus with expected ledgers, live agent-student sessions, S1–S5 LLM stage audits, Fable scorecard adjudication.
**Runs:** F1b (first live run, `campaign/out/f1/`), tuning-fix wave (commits `3cf239f`, `3e38f25`, `7604612`, `cb8b6da`), F1c (re-run, `campaign/out/f1c/`).
**Mandate:** diagnosis, not repair. Nothing below has been "fixed" beyond what was required to make measurement possible; each item lists evidence, severity, and owning subsystem.

---

## 0. Executive summary

The full pipeline works end-to-end as *plumbing*: 31/38 live sessions produced paired artifacts (canonical LLM + graph shadow), real scorecards with varied bands, mastery events, and classroom aggregates. The graph grader itself, however, **abstained on 31/31 attempts** — no longer for incidental reasons (those were found and fixed) but for one clean, confirmed cause: **resolver recall (`unresolved_rate_above_threshold`)**. Around that core blocker sit one design-level correction (misconception architecture), two crash-grade defects, a dead misconception-detection path, an inflation-prone LLM fallback, and a WU-AAS ingestion path that mints graphs the grader cannot consume. The system is a working instrument with a known parts list — none of the parts is mysterious, and every one is now measurable.

---

## 1. Design-level finding

### D1 — Misconception bank architecture is wrong (pre-seeded vs emergent) — **DESIGN, blocks promotion semantics**
- **Intended design (product owner, 2026-07-02):** misconceptions must NOT be pre-authored/seeded. They are **emergent**: built from real student interactions, aggregating canonical per-subtopic scores **across the class** (canonical subjects are global per class). The store therefore needs a per-class (search-space) dimension and an accumulation/promotion mechanism fed by canonical grading artifacts — converging with the deferred trust-gradient pipeline (2026-06-23 decision).
- **Observed defect that revealed it:** the grader treats an empty `apollo_misconceptions` bank as a reason to **abstain from grading entirely** (`misconception_bank_empty` on 36/36 F1b attempts). Under the emergent design, an empty bank is the *normal cold-start state of every class* — abstaining on it is wrong. Coverage grading and misconception detection are separable signals; the artifact schema already separates them.
- **Consequences:** (a) empty bank must degrade to "assert no misconceptions, grade coverage normally"; (b) the campaign seeder (`3e38f25`, `scripts/seed_apollo_misconceptions.py`) is a **harness workaround only** — do not productionize; (c) the WU-AAS path minting no bank (`soundness_applicable=False`) is consistent with the emergent design, not a gap.
- **Owner:** apollo grading orchestration + persistence; design work required before code.

---

## 2. Blockers on the graph path

### G1 — Resolver recall: 100% abstention, single confirmed cause — **CRITICAL, the core blocker**
- **Evidence (F1c, bank seeded, all fixes live):** 31/31 graph rows abstained; reasons histogram: `unresolved_rate_above_threshold` ×31 (+1 co-occurring `min_parser_confidence_below_threshold`). Normalization confidence 0.90–1.00 on every packet — normalization is *fine*; **resolution** is failing. Graph composites for strong personas: 0.15–0.46 (bands all Beginning) vs LLM 0.61–0.95.
- **Interpretation:** this is the known MECH-B / G2 recall problem, now measured cleanly at corpus scale on two subjects. The lexical→derived→NLI ladder resolves too few student nodes against reference nodes even for personas scripted directly from the reference graph's own keys.
- **Existing direction:** the clarification loop was designed as the G2 fix — but see G2 below: it produced zero observable clarifications in this corpus.
- **Owner:** `apollo/resolution/` + clarification loop. This is where tuning/iteration effort goes first; graph-graded fraction stays 0% until it moves.

### G2 — Clarification loop produced zero observable effect — **CRITICAL**
- **Evidence:** `clarification_trace`/`clarifications` empty on all 31 artifacts and scorecards, including all vague-then-clarifies personas (e.g. attempts 27, 47 — scored 76/93 with no trace). S4 (Apollo-coherence) 35.5% vs 90% bar. `APOLLO_CLARIFICATION_ENABLED=1` was set on the stack.
- **Possible causes (undiagnosed — needs targeted probe):** loop not firing on this route; firing but trace not persisted to artifacts; or the driver's '?'-heuristic never engaging it. Any of the three is disqualifying for the clarify-then-fallback live flow the spec mandates.
- **Owner:** apollo clarification handlers + `campaign/cast/student.py` heuristic (joint).

### G3 — A shadow-grader crash 500s the student's Done request when LIVE is off — **HIGH**
- **Evidence:** linear_motion F1c chunk aborted systemically: `POST /apollo/sessions/{35,36}/done → 500`, traceback `KeyError: 'variable_mapping'` in `run_graph_simulation` (`done_grading.py:303` path). With `APOLLO_GRAPH_GRADER_LIVE=0` the A4 any-exception fallback deliberately does not engage (byte-identity contract), so a *shadow* failure kills the *live* grade.
- **Why it matters:** shadow mode is exactly the mode staging/prod will run during calibration; shadow must never be able to fail the student request. The fallback/isolation needs to cover shadow-mode exceptions too (log + skip shadow, serve LLM grade) without violating the byte-identity requirement on the served payload.
- **Owner:** `apollo/handlers/done.py` shadow-chain error handling.

### G4 — WU-AAS-minted subjects are not consumable by the graph grader — **HIGH (blocks the teacher-authored path)**
Four distinct sub-findings from the live PDF run (evidence: `campaign/out/f1/provisioning-notes.md`, F1c linear_motion crash):
1. **Missing `variable_mapping`:** minted problems lack the payload `run_graph_simulation` assumes exists on every problem (seeded subjects always have it) → the G3 crash. Either mint must produce it or the grader must tolerate its absence.
2. **Equation parser gaps at mint:** gate rejections of chained equalities and `^` notation; `x` rejected as a "foreign symbol" — real-world teacher PDFs will hit all of these constantly.
3. **Entity duplication:** 37 entities minted for 2 problems, 2–4 ambiguous duplicate candidates per role, no `equation` payload to disambiguate — made honest persona-key reconciliation impossible (kept PROVISIONAL).
4. **No ingestion observability:** the authored-sets API exposes no page-level OCR text/confidence, `apollo_ingest_runs`/`errors` stay empty → S2 audit ran on thin inputs (25%, "insufficient info" failures, not proven defects). The S2 gate is unmeasurable until ingestion captures its own raw evidence.
- **Owner:** WU-AAS provisioning (`apollo/provisioning/authored_sets/`, tag mint, OCR pipeline).

---

## 3. Quality gaps (measurable now, below bars)

### Q1 — Misconception detection asserted nothing, corpus-wide — **HIGH**
S5: 0 assertions / 38 attempts (vacuous both runs — F1b from the empty bank, F1c *with* bank seeded). All three misconception personas (attempts 3, 4, 11 in the packets) taught their scripted misconception verbatim (bank `trigger_phrases` used verbatim in the beats) and received scorecards with **empty `watch_out`** — graded Proficient/Developing with no warning. Detection appears gated behind resolution success (G1) and/or the D1 abstention logic. Whatever the mechanism: the signal the classroom projection and the pedagogy most depend on is currently silent. **Owner:** resolution/misconception matching; blocked partially by G1/D1.

### Q2 — Evidence spans are empty in every scorecard — **MEDIUM**
Every `taught_well[].evidence_span` is `""` (all 12 packets). The canonical LLM path has no per-node utterance spans (structural), but rendering empty strings violates the artifact contract's purpose ("in the student's own words") and makes the S3 fidelity audit weaker than designed (S3 63.9% vs 95%). Either the LLM path renders without the span field, or spans get sourced from `per_step` matching. **Owner:** `artifact_build.build_llm_artifact` + scorecard renderer.

### Q3 — LLM-path grade inflation vs expected ledgers — **MEDIUM**
Partial-knowledge personas (scripted to teach roughly half their nodes) scored **93/Strong** twice (attempts 38, 47); a strong persona scored 61/Developing (attempt 21) — the LLM rubric grade is noisy against ground-truth personas in both directions. This is today's *live* grader. The paired corpus now quantifies it (band-agreement metric in `campaign/report.py`); worth tracking as the baseline the graph path must beat. **Owner:** overseer rubric (informational until promotion comparisons).

### Q4 — Reference-graph quality: S1 at 75.1% vs 95% bar — **MEDIUM**
172/229 items pass across the three subjects (fluid 73–75%, macro ~71–75% band across runs). Failures include duplicate/near-duplicate nodes and edges judged untrue against source material. Partly judge-calibration (untuned prompts), partly real duplication (see G4.3 for the WU-AAS extreme). Needs one calibration pass separating "judge too harsh" from "graph actually wrong" before the bar is meaningful. **Owner:** subject seed data + judge prompt calibration.

### Q5 — Fable adjudication: 5/11 sane vs 95% bar — **summary signal**
Not-sane patterns (each mapped above): misconception personas with empty `watch_out` (×3 → Q1), vague personas with high scores and no clarification trace (×2 → G2), partial personas at Strong (×2 → Q3). The sane six were coherent: bands matched rubric, missing_or_unclear listed real reference keys with readable guidance (the fbc6d8d ledger fix working live).

---

## 4. Ops gaps

### O1 — Done-click grading latency: p95 29.1s (max 42.3s) vs 15s bar — **MEDIUM**
Worse than F1b's 21s (NLI + clarification flags on, CPU inference in the loop's thread offload, plus double grading in paired mode). Needs profiling before promotion; note prod won't pay the paired-capture cost twice unless shadow stays on. NLI pre-warm works (6.5s warm start, offline-cache verified) — first-request download is solved.

### O2 — Environment fragility (host-level, documented) — **LOW**
torch on Windows requires the pinned `2.6.0+cpu` venv (README'd); Supabase analytics container can't run on Windows Docker Desktop (disabled in harness config); two isolated 422 `malformed_equation`/chat errors and one persona 422 remain unexplained per-attempt failures worth a look (~3/38 ≈ 8% attempt attrition).

---

## 5. What the campaign proved WORKS (don't re-litigate)

- **Canonical artifact pipeline end-to-end:** 2 rows per attempt (31/31 exact), UNIQUE(attempt_id, role) idempotency, mastery events + classroom aggregates fed from artifacts, scorecards rendered pure-template. Band distribution real after `3cf239f`: 15 Strong / 5 Proficient / 8 Developing / 3 Beginning, mean composite 0.75, one legitimate zero.
- **The three tuning-wave fixes, live-verified:** LLM artifact reads real `per_step` coverage; misconception bank seedable (as harness tooling); NUL bytes sanitized at grading persistence (F1b's 500 did not recur).
- **Harness reproducibility:** fresh-boot → reset → re-provision → re-run, twice, with frozen config snapshots and tamper detection.
- **The instrument itself:** stage audits + expected-ledger personas + paired artifacts + adjudication packets successfully isolated every failure above to a subsystem. This is reusable for every future tuning round.

## 6. Ordered checklist — before the graph path becomes the main system

1. **[D1] Redesign misconception store** as emergent per-class aggregation over canonical artifacts (brainstorm first — trust-gradient pipeline decision pending); remove abstain-on-empty-bank as part of it.
2. **[G1] Resolver recall program** — the long pole. Use this corpus as the fixed benchmark; target unresolved_rate below the abstention floor on strong/partial personas before anything else.
3. **[G2] Clarification loop probe** — one targeted session with logging to determine fire/persist/heuristic failure; then fix; it is both a product feature and the designed G1 mitigation.
4. **[G3] Isolate shadow-path exceptions** from the live request (log + skip, never 500) while preserving served-payload byte-identity.
5. **[G4] WU-AAS mint compatibility:** `variable_mapping` contract, parser tolerance (chained eq / `^` / `x`), dedup at mint, page-level OCR capture for auditability.
6. **[Q1] Misconception detection** re-verified non-vacuous once G1/D1 land (S5 gets its first real precision number).
7. **[Q2] Evidence spans** on the LLM path (or render-without) — restores the artifact's evidence contract.
8. **[Q4] S1 judge calibration pass** + subject-graph dedup; then trust the 95% bar.
9. **[O1] Latency profiling** to ≤15s p95 with NLI on.
10. **[Q3] Keep the paired band-agreement baseline** running in every future round — it is the promotion criterion's denominator.

## 7. Run/branch inventory (for the eventual PRs)

- `feat/apollo-canonical-artifact` (main checkout): Phases A+B, 10 commits `6849cab..436de08`, suite 2713 passed, diff-cover 100%, migrations 034/035 local-only.
- `feat/apollo-e2e-campaign-harness` (worktree, includes merge `b7913c9` of the above): harness C1–C3, cast D1–D3, judges E1, report E3, tuning fixes, run outputs `campaign/out/f1/` + `f1c/`.
- Full run data: `attempts.jsonl`, judge JSONs, sanity output, adjudication packets — all committed under `campaign/out/`.
- Not addressed by design: F2 freeze/held-out gate run (cancelled — this document replaces it as the session deliverable).
