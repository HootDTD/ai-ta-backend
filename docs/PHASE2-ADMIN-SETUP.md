# Phase 2 — Admin setup (your action required)

> The CI/CD **code** (workflows, composite action, coverage gate, ruff/mypy
> config, pre-commit, dependabot, `.gitattributes`) is in PR — see
> `test/phase2-cicd`. The steps below need **repo-admin** + **Heroku-dashboard**
> access and cannot be done from the workflow code. Do them in this order.
>
> Repo: `HootDTD/ai-ta-backend` (private). Prod branch: **`ApolloV3`** (kept — Heroku
> deploys from it). Required status check name: **`ci-passed`**.

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

## 3. Heroku deploy gate (Dashboard)

Heroku deploys from the connected branch via the GitHub integration, **not** via
Actions. Make Heroku wait for our gate:

1. Heroku Dashboard → your app → **Deploy** tab.
2. **Deployment method:** confirm GitHub, connected to `HootDTD/ai-ta-backend`.
3. **Automatic deploys:** confirm the branch is **`ApolloV3`**.
4. ☑ **"Wait for CI to pass before deploy"** — this ties Heroku auto-deploy to the
   `ci-passed` check on `ApolloV3`. (Auto-approve preserved: it deploys itself once
   CI is green — no manual reviewer.)
5. Verify there's exactly **one** prod app pointed at `ApolloV3` (no stale app on an
   old branch name). Do this **before** any future branch rename.

---

## 4. Prove the promotion chain (dry run — plan §2.11)

Push one trivial change through the full chain to prove the gates work end-to-end
before relying on them:

```
feature/ci-smoke ── PR ──▶ staging   (ci-passed must be green to merge)
        staging  ── PR ──▶ ApolloV3   (ci-passed green ▶ Heroku auto-deploys)
```
Confirm: PR blocked while CI runs → unblocks on green; Heroku deploy fires on the
`ApolloV3` merge; `/healthz` returns OK post-deploy.

---

## 5. Done when

- [ ] Phase 2 PR merged to `staging`; `ci-passed` green once.
- [ ] Rulesets active on `staging` + `ApolloV3`; `ci-passed` is the required check.
- [ ] `production` Environment exists; prod secrets scoped to it; no Env reviewer.
- [ ] Heroku "wait for CI" on; connected branch confirmed = `ApolloV3`.
- [ ] Dry-run promotion proven; `/healthz` green post-deploy.
