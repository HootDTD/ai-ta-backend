"""Apollo E2E grading campaign harness.

Fully local-Docker infrastructure (Supabase local stack + Neo4j container) and
the campaign cast/judges/orchestration built on top of it. See
``docs/superpowers/plans/2026-07-01-apollo-e2e-campaign-plan.md`` (Phase C-F)
and ``docs/superpowers/specs/2026-07-01-system-scores-outputs-design.md`` for
the authoritative design. Nothing in this package touches remote Supabase or
Neo4j Aura — see ``campaign/README.md`` for the local bring-up procedure.
"""
