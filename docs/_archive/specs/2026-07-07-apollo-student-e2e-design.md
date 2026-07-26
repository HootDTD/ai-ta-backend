# Apollo Student-Facing E2E Baseline — Design

**Date:** 2026-07-07
**Goal window:** 48 hours
**Status:** Approved (brainstorm 2026-07-07)

## Goal

Make Apollo baseline-functional end-to-end from the student UI: real students,
unsupervised, on staging, in a **new course whose materials are not yet
uploaded**. Core loop must be solid; non-critical rough edges may be parked.

Success = a student who has never seen the product can: redeem an invite link →
join the course → open the Apollo section → pick a concept, difficulty, and
problem → teach Apollo in chat → click Done → resolve any done-gate review →
receive a graded report → continue to a next problem → see their progress
dashboard move. No dead ends, no raw error strings.

## Context findings (what shaped this design)

- The Hoot-conversation dependency of Apollo session creation is **one line**:
  `infer_concept_id(transcript, candidates)` in
  `apollo/hoot_bridge/session_init.py`. Everything else (problem selection,
  difficulty, session/attempt rows) is DB-driven and parameterized. The F2
  campaign validated the full backend teach→grade loop this weekend using
  synthetic transcripts.
- The student UI already handles: session load, chat, done → report, retry,
  end, XP greeting. Gaps: no non-Hoot entry, no next/restart buttons (backend
  endpoints exist; no proxy routes), done-gate 422 renders a generic error
  (`DoneGateModal` exists but is never mounted), KG panel is mounted read-only
  (`ApolloKGPanel` without `sessionId` — the negotiation pills are fully built
  dead code).
- The done-gate's resolution mechanism **is** KG negotiation
  (challenge/paraphrase/skip then re-Done), so shipping the done-gate flow
  requires mounting the interactive KG panel. Both are wiring jobs, not builds.
- Invite links are fully built across backend (`server.py`
  create/list/revoke/resolve/redeem), teacher UI management, and student UI
  `join/[code]` page. Verify-on-staging job, not a build job.
- `GET /apollo/progress` returns XP/level/title/threshold only. Per-concept
  mastery lives in `apollo_learner_state`, populated by `_project_mastery`
  (done.py) **only when `APOLLO_GRADING_ARTIFACT_ENABLED=1`**.

## Decisions

1. **Entry point: standalone Apollo section.** Students pick concept +
   difficulty + specific problem. No Hoot conversation required. The Hoot
   "Teach Apollo" button remains untouched (works today; not the only door
   anymore).
2. **Must-haves:** problem browsing, done-gate review flow, progress
   dashboard, and (as a dependency of the done-gate) interactive KG
   negotiation. **Parked:** anything beyond that (clarification probes,
   shadow/NLI/live graph grading, emergent misconceptions, KG-negotiation
   polish beyond what the done-gate needs).
3. **Flag posture on staging:** `APOLLO_DONE_GATE_ENABLED=1`,
   `APOLLO_GRADING_ARTIFACT_ENABLED=1` (feeds scorecard + mastery projection).
   All other Apollo flags stay at defaults (off), so the only LLM calls on the
   student path are the campaign-validated ones.
4. **Mastery data source:** the artifact-flag projection (composite EWMA into
   `apollo_learner_state`). Zero new write paths; mastery bars start empty
   until a student grades attempts. Accepted trade-off.
5. **Sequencing principle:** all code is built and verified against the
   existing F2 linear-motion staging course *before* the new course's
   materials arrive, so content provisioning lands on a proven pipeline.

## Backend additions (ai-ta-backend)

Three new endpoints + one payload extension. All reuse existing internals;
none touch grading, chat, or retrieval logic. All are course-membership
guarded like existing Apollo routes.

1. **`GET /apollo/concepts?search_space_id=`** — wraps existing
   `list_course_concepts`. Returns `[{concept_id, display_name}]`.
2. **`GET /apollo/problems?search_space_id=&concept_id=&difficulty=`** — thin
   query over `apollo_problems` using the same eligibility predicate as
   `select_problem_personalized` (promoted/eligible only). Returns
   `[{id, difficulty, problem_text, attempted}]` where `attempted` = the
   requesting student has a prior `ProblemAttempt` for it.
3. **`POST /apollo/sessions`** — body
   `{search_space_id, concept_id, difficulty, problem_id?}`. Same as
   `init_session_from_hoot` minus `infer_concept_id`. If `problem_id` is
   given: validate it belongs to the concept + course, use it; else fall back
   to `select_problem_personalized`. Same end-any-active-session semantics and
   same response shape (`{session_id, attempt_id, problem}`), so the
   downstream UI session flow is unchanged.
4. **`GET /apollo/progress` grows a `detail` block** — per-concept mastery
   (read `apollo_learner_state`) and recent attempts (concept, difficulty,
   grade, timestamp). Reads only; no new tables.

## Student UI additions (ai-ta-student-ui)

**Browse screen** (`/apollo`, shown when no active session): concept list →
difficulty tabs → problem cards (text preview, "tried" badge), "Start
teaching" per card + "Surprise me" (omits `problem_id`). Compact progress
header (level, title, XP-to-next) linking to the dashboard. New proxy routes
for the two list endpoints and `POST /sessions` (mechanical copies of the
existing eleven).

**Session screen** (existing `ApolloPageClient`, three wiring changes):

1. Mount `ApolloKGPanel` with `sessionId` + `onKgUpdated` (+ `pulseEntryId`)
   so the built challenge/paraphrase/skip/trace pills go live.
2. **Next problem** button (on the report screen after grading) and
   **Restart** button (session header, behind a confirm) — new proxy routes
   for `/next` and `/restart_problem`, which have none today.
3. `handleDone` catches the 422 `review_required` body and mounts the existing
   `DoneGateModal`: jump-to-entry scrolls the KG panel and opens the dispute
   card; the touched-set fills as negotiation moves succeed; re-Done enabled
   when all flagged entries are touched. `ApolloErrorSurface` also gets a
   proper `review_required` case as a fallback so that path can never render
   "Something went wrong."

**Progress dashboard** (`/apollo/progress` page): level/XP header, per-concept
mastery bars, recent-attempts list. Empty state = "teach your first problem,"
not bare zeros.

## Content pipeline & onboarding (non-code half)

- **Phase 1 — build + verify on existing content (first ~day):** everything
  above, validated on the F2 linear-motion staging course with a test student:
  browse → pick problem → teach → done-gate fires (seed one disputed entry) →
  resolve via KG moves → grade → next problem → dashboard moves.
- **Phase 2 — new course content (when materials land):** teacher-upload the
  corpus through the real teacher flow → run provisioning manually (respect
  the single-drainer rule: confirm no worker holds the lease first) →
  spot-check promoted problem quality/count per concept → thin concepts get
  more source material or are parked.
- **Phase 3 — onboarding + launch:** create invite link in teacher UI →
  redeem as a fresh account in a clean browser → run the full student journey
  once → hand links to students.

**Known landmines:** staging has no OCR configured — if materials include
handwriting/scans, set `OCR_PROVIDER=openai` before upload. Provisioning
promotion rates on an unseen corpus are the one unpredictable step; it is
isolated in Phase 2 with a fallback (launch on fewer concepts if needed).

## Verification

- Unit tests for the three new endpoints using existing Supabase mock fixtures
  (conftest.py patterns), including membership guards and the
  `problem_id`-validation reject path.
- Proxy-route smoke script for the new student-UI routes.
- Phase 1 manual E2E is the real gate; the done-gate + KG flow is exercised
  with the flag on in staging before any student sees it.
- Each phase ends shippable: Phase 1 alone = a working Apollo section on
  existing staging content.

## Out of scope (explicitly parked)

- Clarification probes (`APOLLO_CLARIFICATION_ENABLED` stays off)
- Shadow / NLI / live graph grading, emergent misconceptions (flags off)
- KG negotiation polish beyond what the done-gate flow needs
- Any Hoot-side changes (the "Teach Apollo" button keeps working as-is)
- Production environment (staging only)
- Removing `from_hoot` (kept; standalone entry is additive)
