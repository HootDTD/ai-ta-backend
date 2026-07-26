# `_archive/` — parked transient docs

This tree holds **transient** documents: point-in-time records that described a
task while it was in flight, not the durable system as it is now. It is committed
(so the record survives) but **quarantined from navigation** — the doc tree's
reading protocol never routes here.

## Lifecycle rule

- **What lives here:** handoffs, implementation plans, design specs, run logs,
  experiment writeups, research memos, design/brainstorm notes, and audits — any
  markdown that captured *a moment in the work* rather than *the current interface*.
- **Do NOT load `_archive/` by default.** Open a file here only when you are
  resuming the exact task whose record it is. The durable truth for how the system
  works today lives in `architecture/**` (owner docs) and `shared-architecture/**`
  (cross-repo). If an `_archive` doc disagrees with code, code + the durable tree
  win — the archived doc is a snapshot, not an authority.
- **Adding here:** when a task's handoff/plan/spec/run is done, drop it in the
  matching bucket below with a `YYYY-MM-DD-` filename prefix. Never leave transient
  markdown loose in a `docs/` root and never place it in `architecture/`.
- **Graduating out:** when a spec's design becomes real, move its durable content
  into the owning `architecture/` leaf (the drift contract) and leave the spec here
  as the historical record.

## Buckets (dated index)

| Bucket | Holds | Files | Span |
|---|---|---|---|
| `handoffs/` | task-to-task context handoffs + runbooks | 11 | 2026-06 → 2026-07 |
| `plans/` | implementation / phase plans (mostly the apollo-KG `wu*` chain) | 70 | 2026-04 → 2026-07 |
| `specs/` | design specs (apollo v2/v3, KG, grading, provisioning) | 28 | 2026-04 → 2026-07 |
| `runs/` | overnight / campaign run logs | 1 | 2026-04 |
| `experiments/` | experiment writeups + result sets (some are sub-dirs) | 22 | 2026-06 → 2026-07 |
| `research/` | prior-art / review memos | 1 | 2026-07 |
| `design/` | design + brainstorm notes; the demoted legacy data-flow reference | 3 | 2026-06 → 2026-07 |
| `audits/` | audit records | 2 | 2026-06 |
| (root) | `claude_v3_checklist.md` — retired Apollo-V3 flaws checklist | 1 | — |

## 2026-07-25 quarantine batch (granular doc-restructure, W0a)

The granular architecture-doc restructure moved these into `_archive/` so that
`architecture/` could become purely durable. Recorded here so they stay findable:

- `docs/superpowers/plans/**` → `plans/` (55 files) — the superpowers tool's plan output.
- `docs/superpowers/specs/**` → `specs/` (11 files) — the superpowers tool's spec output.
- `docs/superpowers/runs/**` → `runs/` (1 file: `2026-04-15-overnight-results.md`).
- `docs/audits/**` → `audits/` (2 files).
- `docs/design/**` → `design/` (1 file: `2026-06-23-apollo-soundness-na-sentinel.md`).
- `docs/DATA-FLOW.md` → `design/data-flow-legacy.md` — **demoted + renamed.** The 991-line
  monolith is superseded by the thin `shared-architecture/data-flow.md` router; kept
  here for its pre-#194 schema/proxy detail only.
- `docs/TESTING-CI-PLAN.md` → `plans/TESTING-CI-PLAN.md` — Phases 0-2 done; durable CI
  facts graduate into `architecture/platform/ci-workflows.md`.
- `docs/TESTING-CI-HANDOFF.md` → `handoffs/TESTING-CI-HANDOFF.md`.
- `docs/apollo-redesign.md` → `specs/apollo-redesign.md` — durable content already
  realized in the apollo leaf tree.
- `docs/claude_v3_checklist.md` → `_archive/claude_v3_checklist.md` (root).

The former root docs `branching.md` and `PHASE2-ADMIN-SETUP.md` were **promoted**
(not archived) into `shared-architecture/` (`branching.md`, `admin-setup.md`) as
durable cross-repo references.
