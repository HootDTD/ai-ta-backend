# Handoff: Apollo feedback transcript + narrative sanitization (build in progress)

- **Date:** 2026-07-12
- **Feature:** (1) attempt-scoped chat transcript on the Done payload + collapsed dropdown on the student report; (2) diagnostic narrative stops leaking internals (canonical keys, `credit=`/`weight=` decimals, dock values)
- **Spec (approved):** `docs/_archive/specs/2026-07-11-apollo-feedback-transcript-sanitize-design.md` → commit `f2c1316`
- **Plan (7 tasks, exact code per step):** `docs/_archive/plans/2026-07-11-apollo-feedback-transcript-sanitize-plan.md` → commit `c1d9348`
- **Process:** superpowers subagent-driven-development; ledger at `.superpowers/sdd/progress.md`; per-task briefs/reports/diffs in `.superpowers/sdd/`

## Where the work lives

| Repo | Worktree | Branch | State |
|---|---|---|---|
| ai-ta-backend | `TA-test/.worktrees/feedback-ux` | `feat/apollo-feedback-transcript-sanitize` | HEAD `af62103`, clean tree, rebased onto `origin/staging` @ `82ae600` (includes PR #139 Neo4j degraded) |
| ai-ta-student-ui | `TA-test/.worktrees/feedback-ux-ui` | `feat/apollo-feedback-transcript-sanitize` | = `origin/staging` @ `2a200be`, no commits yet, `npm install` done |

Both main checkouts hold OTHER in-flight work — do not build there. Python for backend commands: `C:/Users/ultra/OneDrive/TA-test/ai-ta-backend/.venv/Scripts/python`, run from the worktree root (imports `apollo` from cwd — verified).

## Completed

**Task 1 — `sanitize_narrative` gate (COMPLETE, review clean after 1 fix loop).**
Commits `1166c6a` + `64f997d`. Pure, idempotent `sanitize_narrative(text, canonical_keys=()) -> str` in `apollo/overseer/topic_narrative.py`, exported in `__all__`; strips canonical keys, 0–1 scoring decimals (`credit 0.80`, `weight 1.0`, `dock: 0.000` incl. parentheticals), preserves whole-number percentages, `$...$` math, and out-of-range prose ("weight 1.5 N", "weight = mg"); final `.strip()`. 13 tests in `apollo/overseer/tests/test_topic_narrative_sanitize.py`, 100% module coverage, ruff clean. Reviewer-accepted Minors deferred to final whole-branch review (see ledger): >1.0 near-miss digit stubs ("credit 1.05" → "5"), contrived adjacent-keyword idempotency, mid-sentence grammatical stubs.

**Task 2 — prompt drops internals (IMPLEMENTED + review Approved; one 2-line textual fix PENDING).**
Commit `af62103`. `_format_topic_line` now emits `Topic "display name": status — NN%` (no keys/decimals); new `_humanize_key` fallback; user message drops `Coverage component`/`Misconception dock` lines (keeps `Score: 64 (C)`); system prompt gained the NEVER-internals HARD RULE (whole-number percentages allowed). New `apollo/overseer/tests/test_topic_narrative_prompt.py`; updated stale assertions in `test_diagnostic_topic_score.py` (per plan) and `test_topic_narrative.py` (out-of-brief but reviewer-verified legitimate contract inversions). Suite: overseer 587 passed; skip-count jump 2→13 was verified to be Docker-down environment skips, NOT diff-caused.

**PENDING fix for Task 2 (dispatched, agent stopped by user before it wrote anything — working tree is clean):** two text edits in `apollo/overseer/topic_narrative.py`, no behavior change:
1. `_TOPIC_SYSTEM_PROMPT` framing paragraph (~line 29) still says "...evidence quote and point cost" — contradicts the HARD RULE; rewrite to "...evidence quote and whether it was corrected".
2. Module docstring (~line 9) still claims "status/credit ... evidence span + dock points are named explicitly in the prompt" — update to the new contract (status + whole-number %; internals never reach the prompt).
Commit message: `fix: align narrative prompt framing and docstring with no-internals contract`. Cover with existing tests: `pytest apollo/overseer/tests/test_topic_narrative_prompt.py apollo/overseer/tests/test_topic_narrative.py apollo/overseer/tests/test_diagnostic_topic_score.py -q` + `ruff check apollo/overseer/topic_narrative.py`. Then re-review (task-2 reviewer context is gone; a fresh reviewer only needs the delta diff).

## Remaining (plan Tasks 3–7 — exact code in the plan)

3. Wire `sanitize_narrative` into `generate_diagnostic` (`apollo/overseer/diagnostic.py`) as last step before return + `test_diagnostic_sanitize.py` + reconcile `docs/architecture/apollo.md` (same commit).
4. `_fetch_attempt_transcript` helper + `student_response["transcript"]` in `apollo/handlers/done.py`; add the stub patch to `_old_path_patches` (`test_done_shadow_flag.py`); add `"transcript": []` to the two full-payload goldens (`test_done_graph_grader_live.py` ~130, `test_done_shadow_isolation.py` ~72); new `test_done_transcript.py`; reconcile `apollo.md`. NOTE: plan line anchors predate the #139 rebase — locate by content, not line number.
5. Full `pytest apollo` + `--cov` + `diff-cover --compare-branch=origin/staging --fail-under=95` + ruff (the 95% gate has deliberately not been run yet).
6. UI worktree: `TranscriptTurn` type + `transcript?` on `DoneResponse` (`lib/apollo/api.ts`), collapsed `<details>` `TranscriptSection` in `components/apollo/ApolloReportPanel.tsx`, CSS in `app/globals.css`; verify `npx tsc --noEmit` + `npm run lint`; reconcile `components.md` + `_overview.md`. No test runner — list untested changes in the PR description.
7. Local e2e smoke (backend :8000 from worktree + UI :3001), one session to Done: dropdown shows this attempt's turns; narrative has no internals.

Then: final whole-branch review (most capable model; feed it the deferred Minors in the ledger), and two PRs → `staging` (backend + UI) per superpowers:finishing-a-development-branch.

## Provenance / evidence

Live defect this fixes: staging attempt 62 (MGMT course, search_space 5, TEST Supabase `hjevtxdtrkxjcaaexdxt`); served narrative contained `(proc_explain_directionality, credit 0.80, weight 0.77)` etc.; artifact rows `apollo_grading_artifacts` ids 43/44. Transcript decision: only the graded attempt's turns (roles `student`/`apollo`, ordered by `turn_index`).
