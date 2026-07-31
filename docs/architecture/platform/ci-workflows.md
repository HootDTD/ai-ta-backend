---
doc: platform/ci-workflows
description: The GitHub Actions CI/CD surface plus repo build-meta (pytest/ruff/mypy/coverage/requirements/pre-commit/package.json) — how the backend builds, tests, and gates PRs
owns:
  - .github/workflows/ci.yml
  - .github/workflows/nightly.yml
  - .github/workflows/backmerge.yml
  - .github/actions/setup/action.yml
  - .github/dependabot.yml
  - pytest.ini
  - .coveragerc
  - requirements.txt
  - requirements-test.txt
  - .pre-commit-config.yaml
  - mypy.ini
  - ruff.toml
  - package.json
  - scripts/docs/check_owns_coverage.py
  - scripts/docs/owns_exclude.txt
  - .claude/hooks/guard_docs_placement.py
related:
  - database/legacy-migrations
  - database/supabase-migrations
  - reports/ai-use-pdf
  - platform/ops-db-tooling
last_verified: 2026-07-31
stub: false
---

# platform/ci-workflows — CI/CD + repo build-meta

One coherent topic: how the repo builds, tests, and gates. Config files (not
`.py`) — optional-universe ownership, so nothing orphans.

## Interface

**`ci.yml`** (on PR/push to `main`/`staging`/`ApolloV3`) — six jobs:
- `quality` — ruff on **changed files only** (blocking for ADDED, advisory for
  MODIFIED; ~360 legacy findings make a repo-wide gate impossible).
- `typecheck` — mypy ratchet (ADDED blocking, MODIFIED advisory; `mypy.ini`
  `follow_imports=silent` scopes errors to the listed files).
- `unit` — `pytest -m "not integration"`, no Docker.
- `integration` — full suite on pgvector + Neo4j Testcontainers, **plus the
  patch-coverage gate** `diff-cover coverage.xml --compare-branch=origin/$base
  --fail-under=95` (**95%** — matching the CLAUDE.md contract, not 80%).
- `database` — pinned Supabase CLI (2.109.0) drift check + clean local reset.
- `docs` — the architecture ownership lint. Runs `scripts/docs/check_owns_coverage.py`
  (owned here, with its `owns_exclude.txt`); flipped from advisory to **required** at
  W5 (in `ci-passed.needs`, `--last-verified-required` on the PR path). The
  `last_verified` PR-path gate tolerates **same-day stacked PRs**: a leaf that changed
  in the diff and already carries the head commit's author date (`git log -1
  --format=%as`) OR today's UTC date passes without a literal bump — a date-granular
  field can't advance twice in one day, and on `pull_request` events CI checks out
  GitHub's synthetic merge ref, whose author date carries the PR author's local
  offset and can sit a day behind the UTC runner (ported from the student-ui gate;
  UTC-today arm added 2026-07-31).
- `ci-passed` — the aggregation status; **the single required branch-protection
  check** (`docs` deliberately not in its `needs` yet).

**`nightly.yml`** — full suite incl. e2e/slow on a 3.11/3.12 matrix, a ratcheted
PROJECT-floor coverage (`coverage report --fail-under=20`, distinct from the
per-PR PATCH gate), and an advisory `pip-audit`. RAG eval is intentionally NOT
here (nondeterministic, costs money).

**`backmerge.yml`** — on push to `main`, opens/reuses a `main→staging` PR
whenever `main` carries non-merge commits `staging` lacks, so hotfixes are never
forgotten and pure promotions stay silent (per `branching.md`).

**`.github/actions/setup/action.yml`** — the composite action reused by every
job: SHA-pinned actions, pip cache keyed on both requirements files, and the
**weasyprint native pango/cairo libs** (without them `import server` fails before
any test — the dependency `reports/ai-use-pdf` relies on).

**`dependabot.yml`** — weekly Actions (SHA bumps) + pip updates (test-tooling
grouped). **Repo build-meta**: `pytest.ini` (testpaths `tests apollo campaign`,
asyncio auto), `.coveragerc` (`concurrency = thread, greenlet`; excludes live-LLM
smoke harnesses), `ruff.toml`, `mypy.ini`, `requirements*.txt`,
`.pre-commit-config.yaml` (same ruff as CI + the advisory docs lint),
`package.json` (the `db:drift`/`db:reset` Node scripts → `ops-db-tooling`).
**Docs-enforcement tooling** also lives here: `.claude/hooks/guard_docs_placement.py`
(the PreToolUse guard blocking new markdown outside `architecture/`/`_archive/`).

## Invariants & gotchas

- **The patch gate is 95%, not 80%** (verified `ci.yml`); it is skipped on
  promotion PRs where `base=ApolloV3` (the ratchet is enforced on the way INTO
  staging, not re-litigated on promotion).
- **`ApolloV3` is the RETIRED former prod branch** (CLAUDE.md) yet still appears
  in `ci.yml`'s trigger list and the promotion-skip conditions — treat those as
  **stale-in-workflow**, not current truth; do not reproduce them as live config.
- All third-party actions are SHA-pinned with a trailing version comment
  (supply-chain hardening).

## Related

`database/legacy-migrations` + `database/supabase-migrations` (the two chains the
`database` job's drift check guards), `reports/ai-use-pdf` (needs the setup
action's native libs), `ops-db-tooling` (the `package.json` Node scripts).
