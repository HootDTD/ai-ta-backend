# Branching model — pilot era (2026-07)

Applies to all three repos (`ai-ta-backend`, `ai-ta-student-ui`, `ai-ta-teacher-ui`).

## The two branches

- **`staging` = trunk.** All development happens here — Hoot improvements, Apollo
  grading work, everything. Feature branches → PR → staging. Deploys the staging
  Railway environment (staging Supabase + staging Neo4j).
- **`main` = pilot release branch.** Runs the live MGMT 38200 class pilot. All four
  prod Railway services deploy from it, against prod Supabase + the prod (Aura) Neo4j.
  Merging to main IS a deploy — do it when you can watch it come up.

## The three rules

1. **main never gets unique feature work.** It moves only two ways:
   - **Promotion:** a `staging → main` PR when a batch is pilot-ready. Promote the
     whole branch — never cherry-pick individual commits (partial promotions create
     merge states nobody can reason about).
   - **Hotfix:** pilot breaks → branch `hotfix/*` off **main** → PR to main → deploy.
     Then back-merge main into staging immediately so the next promotion doesn't
     re-break prod. `.github/workflows/backmerge.yml` opens that PR automatically.

2. **Promote whole branches; gate behavior with env flags.** Unfinished or risky work
   ships behind env vars that are OFF on prod (the existing `ROUTER_ENABLED` /
   `APOLLO_*_ENABLED` pattern). Code promotes freely; prod's Railway env decides what
   runs. Promote small and often (roughly weekly) — divergence is what makes
   promotions scary.

3. **Prod schema changes only at promotion time.** Every schema change lands as a
   numbered migration file on staging. At promotion: merge the PR, apply the pending
   migrations to prod Supabase, let Railway deploy. Keep migrations
   additive/backward-compatible mid-pilot (add columns with defaults; no
   renames/drops), so the old prod code can boot against the newer schema during the
   deploy window.

## When main and staging need different things

Example: pilot grading. The pilot wants the LLM grader improved *now*; staging is
transitioning grading to a graph-grounded approach. Do NOT resolve this with
divergent branches — resolve it with a **strategy seam**:

- The two behaviors live in **separate modules** behind a runtime switch
  (e.g. `APOLLO_GRADING_MODE=llm|graph`, same pattern as
  `[retired A7 shadow switch]`). Prod runs `llm`; staging runs `graph`
  (or shadow-mode: run both, grade with `llm`, log the diff).
- LLM-grader improvements land on staging (they touch only the `llm` module, so
  they never collide with graph work) and promote normally; urgent ones go
  `hotfix/*` off main, and the back-merge stays clean because both branches share
  the same lineage for those files.
- The graph transition promotes to main as dead code and ships to students as a
  **config flip**, with the LLM grader still in the tree as instant rollback.

**The invariant: staging is always a SUPERSET of main.** Work may *originate* on
main when the pilot demands it — the backmerge guard flows it back within a day.
What is never allowed: code on main that staging will never get, or the same file
evolving differently on both branches. That is the merge-hell state, and it is
always avoidable by putting the two behaviors in two modules with a switch.

## Enforcement

- GitHub branch protection on `main` (all three repos): PRs only, no force pushes.
  Admins can bypass for emergencies — the rule is a guardrail, not a cage.
- Backend CI diff-gates (ruff/mypy "added files") skip promotion PRs
  (`base_ref == main`): their diff vs main is months of code that already passed the
  same gates entering staging (re-judging it re-litigates history — see PR #110).
- `.github/workflows/backmerge.yml` (push to main): opens a main→staging PR whenever
  main carries non-merge commits staging lacks. Promotions add only a merge commit,
  so they stay silent; hotfixes trigger it.

## Sharp edges

- **Railway trigger changes don't deploy.** Flipping a service's tracked branch waits
  for the *next* push — after changing it, trigger a deploy manually if you need the
  code now.
- **Neo4j is per-environment, not per-branch.** Staging must point at its own instance
  (local docker) before graph-writing dev resumes; the Aura instance belongs to the
  pilot.
