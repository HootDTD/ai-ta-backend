"""WU-4B3 §6.11 executable corpus — package marker.

The adversarial-fixture corpus that drives the WU-4B grading chain
(``build_audited_grade -> convert_findings_to_events``, plus
``persist_comparison_run`` for the persistence-touching rows). Pure data + the
deterministic ``audit_fn`` / pre-built ``ResolutionResult`` inputs — NO live LLM,
NO Neo4j, NO resolver call. See :mod:`apollo.grading.fixtures.corpus`.
"""
