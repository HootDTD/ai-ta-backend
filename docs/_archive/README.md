# `_archive/` — Transient Docs (skipped by navigation)

This tree holds **non-durable** docs: handoffs, implementation plans, design
specs, experiment/probe writeups, and overnight run logs. It is committed (so it
is shareable and findable) but is **invisible to architecture navigation by
default**.

> **AI sessions: do NOT load anything under `_archive/` unless you are resuming a
> specific task whose handoff/plan lives here.** For how the system works *now*,
> read `docs/architecture/` and `docs/shared-architecture/` instead.

## The rule

- **Durable docs** describe the system as it is now → `architecture/`,
  `shared-architecture/`.
- **Transient docs** describe work in progress / completed work kept for the
  record → here, under the bucket that matches their type.
- When a spec's design becomes real, graduate its durable content into
  `architecture/` (per the drift-prevention contract) and leave the spec in
  `specs/` as the historical record.

## Buckets

| Bucket | Holds |
|---|---|
| `handoffs/` | Phase/session handoff notes |
| `plans/` | Implementation plans |
| `specs/` | Design specs |
| `runs/` | Overnight / batch run logs |
| `experiments/` | Probes and investigation writeups |
| `research/` | Prior-art / research memos |
| `design/` | Design explorations not yet built |

Buckets mirror the pre-quarantine folder names; a later triage pass may
consolidate them.
