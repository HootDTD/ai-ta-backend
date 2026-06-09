# Phase 2 — Admin setup (your action required)

> The CI/CD **code** (workflows, composite action, coverage gate, ruff/mypy
> config, pre-commit, dependabot, `.gitattributes`) is in PR — see
> `test/phase2-cicd`. The steps below need **repo-admin** + **Railway-dashboard**
> access and cannot be done from the workflow code. Do them in this order.
>
> Repo: `HootDTD/ai-ta-backend` (**public** as of 2026-06-09; was private). Prod
> branch: **`ApolloV3`**. Required status check name: **`ci-passed`**.
>
> **2026-06-09 status:** §0 ✅ (PR #4 merged, `ci-passed` green) · §1 ✅ (rulesets
> active via API — 0 approvals both branches, no linear-history: the promotion
> flow uses merge commits) · §2 moot for now (no Actions secrets exist; revisit
> when a deploy job appears) · §3 **rewritten for Railway** (Heroku abandoned —
> last Heroku deploy 2026-04-07, integration dead) · §4 pending Railway hookup.

---

## 0. Prerequisite — let the Phase 2 PR run once

Merge order matters: **don't** turn on branch protection until you've seen the new
`ci.yml` go green at least once (otherwise you can lock yourself out of merging the
very PR that fixes CI). Sequence:

1. Open + merge the Phase 2 PR (`test/phase2-cicd → staging`).
2. Confirm the `ci-passed` check appears and is green on that PR.
3. *Then* apply the branch-protection rulesets below.

⚠️ **First container CI run:** the `integration` job pulls `pgvector/pgvector:pg16`
+ `neo4j:5.25` for the first time in CI. Watch that run for image-pull/boot
timeouts (handoff §9). If it flakes, bump `timeout-minutes` or container wait.

---

## 1. Branch protection (rulesets) on `staging` + `ApolloV3`

**Goal:** require the single `ci-passed` check + PR + linear history; block
force-push/deletion. Solo-team calibration: **0 required reviewers on `staging`**,
**1 on `ApolloV3`** (loosen to 0 if you're merging prod solo).

### Option A — Web UI (no `gh` needed)
`Settings → Rules → Rulesets → New branch ruleset` for **each** branch:

- **Name:** `protect-staging` (then repeat for `protect-apollov3`)
- **Enforcement status:** Active
- **Target branches:** Add target → `staging` (resp. `ApolloV3`)
- **Rules — enable:**
  - ☑ Require a pull request before merging
    - Required approvals: **0** for staging / **1** for ApolloV3
  - ☑ Require status checks to pass
    - ☑ Require branches to be up to date before merging
    - Add check: **`ci-passed`** (type the name; it appears after the first run)
  - ☑ Require linear history
  - ☑ Block force pushes
  - ☑ Restrict deletions

### Option B — `gh api` (if you install GitHub CLI)
Save as `ruleset-staging.json` (swap `staging`/`0` → `ApolloV3`/`1` for prod):

```json
{
  "name": "protect-staging",
  "target": "branch",
  "enforcement": "active",
  "conditions": { "ref_name": { "include": ["refs/heads/staging"], "exclude": [] } },
  "rules": [
    { "type": "pull_request",
      "parameters": { "required_approving_review_count": 0,
        "dismiss_stale_reviews_on_push": true, "require_code_owner_review": false,
        "require_last_push_approval": false, "required_review_thread_resolution": false } },
    { "type": "required_status_checks",
      "parameters": { "strict_required_status_checks_policy": true,
        "required_status_checks": [ { "context": "ci-passed" } ] } },
    { "type": "required_linear_history" },
    { "type": "non_fast_forward" },
    { "type": "deletion" }
  ]
}
```
```bash
gh api -X POST repos/HootDTD/ai-ta-backend/rulesets --input ruleset-staging.json
```

---

## 2. GitHub Environments + scoped prod secrets

**Goal:** prod secrets live ONLY in a `production` environment so a staging
workflow physically can't read them.

`Settings → Environments → New environment`:

| Environment | Deployment branches | Secrets to move here |
|---|---|---|
| `staging`    | `staging` only  | staging-only keys (if any) |
| `production` | `ApolloV3` only | the real `OPENAI_API_KEY`, `SUPABASE_*`, `NEO4J_*` prod creds |

- Set **Deployment branches → Selected branches** to the single allowed branch.
- ⚠️ **Required reviewers / wait timer on Environments need GitHub _Enterprise_**
  on **private** repos. This repo is private on a lower tier, and you chose
  **prod auto-approve**, so **do NOT** add an Environment reviewer — gate prod with
  the branch-protection ruleset (§1) instead. (No required reviewer = auto deploy.)

> The current CI/nightly jobs don't reference an `environment:` yet (they don't
> deploy). When you add a deploy job later, set `environment: production` on it so
> it reads the scoped secrets.

---

## 3. Railway deploy gate (Dashboard)

> **History:** the app originally deployed via Heroku's GitHub integration
> (app `backend-main`); its last deploy was **2026-04-07** and the integration
> is dead/abandoned. Production hosting is moving to **Railway**. If the old
> Heroku app still exists, delete it (or at least disconnect the GitHub repo)
> so two platforms never deploy the same branch.

Railway deploys from a connected GitHub branch, like Heroku did. Wire it so a
red CI can never reach prod:

1. Railway Dashboard → **New Project → Deploy from GitHub repo** →
   `HootDTD/ai-ta-backend`.
2. Service → **Settings → Source**: set the deploy branch to **`ApolloV3`**.
3. Service → **Settings → Deploy**: enable **"Wait for CI"** — Railway then
   waits for the GitHub check suite (our `ci-passed`) to succeed on the commit
   before building/deploying. (Auto-approve preserved: deploys itself once CI
   is green — no manual step.)
4. Set the start command (Railway reads the `Procfile` via Nixpacks, or set it
   explicitly, e.g. `python server.py` / the `web:` line from the Procfile).
5. Add prod env vars in Railway → service → **Variables** (`OPENAI_API_KEY`,
   `SUPABASE_*`, `NEO4J_*`, etc. from `.env.example`). Never commit them.
6. Verify exactly **one** service deploys `ApolloV3`.

---

## 4. Prove the promotion chain (dry run — plan §2.11)

Push one trivial change through the full chain to prove the gates work end-to-end
before relying on them:

```
feature/ci-smoke ── PR ──▶ staging   (ci-passed must be green to merge)
        staging  ── PR ──▶ ApolloV3   (ci-passed green ▶ Railway auto-deploys)
```
Confirm: PR blocked while CI runs → unblocks on green; Railway deploy fires on the
`ApolloV3` merge; `/healthz` returns OK post-deploy. (Until Railway is connected,
the chain stops at the merge — no deploy side effect.)

---

## 5. Done when

- [x] Phase 2 PR merged to `staging`; `ci-passed` green once. *(2026-06-09)*
- [x] Rulesets active on `staging` + `ApolloV3`; `ci-passed` is the required check. *(2026-06-09)*
- [ ] ~~`production` Environment + scoped secrets~~ — deferred: no Actions secrets
      exist yet; prod secrets will live in Railway Variables instead.
- [ ] Railway service connected to `ApolloV3`; "Wait for CI" on; old Heroku app
      deleted/disconnected.
- [ ] Dry-run promotion proven; `/healthz` green post-deploy (once Railway is live).
