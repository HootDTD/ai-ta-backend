"""Tier-1 deterministic equation sign-veto.

Contract: ``docs/_archive/plans/2026-07-08-apollo-misconception-detector-plan.md``
section 5.2, amended by A5 (the bare ``misc.<code>`` this module writes into
``signature`` is what downstream ``merge.py`` copies verbatim into
``canonical_key`` — never re-prefix it here or there).

For each STUDENT equation node in ``student_graph``:

  * if it is sign-EXACT structurally equivalent (reused
    ``apollo.resolution.tiers._symbolic_equiv``) to ANY reference equation ->
    the student is correct -> no finding.
  * else, if it is sign-EXACT equivalent to a pre-authored sign/direction
    MUTANT of a bank equation -- an ``eq:``-prefixed entry in
    ``MisconceptionEntry.trigger_phrases`` -- emit ONE deterministic
    ``misconception`` finding: ``confidence=1.0``, ``corroborated=True``
    (self-corroborating; no other tier needs to agree), ``source="sympy_veto"``,
    ``signature="misc.<code>"``.
  * else no finding at all (an honest non-detection is not a false positive).

Pure and deterministic; no LLM, no IO, no DB. Every SymPy call goes through
``_symbolic_equiv``, which already wraps its own parse/compare in try/except
and returns ``False`` (never raises) on a malformed expression -- this module
inherits that soft-fail contract without adding its own try/except around it,
mirroring the resolver's tier-3 code path.
"""

from __future__ import annotations

from apollo.ontology.graph import KGGraph
from apollo.overseer.misconception_bank import MisconceptionEntry
from apollo.overseer.misconception_detector.types import ConceptFinding
from apollo.resolution.tiers import _symbolic_equiv, student_surface_text

_EQ_MUTANT_PREFIX = "eq:"


def _mutant_equations(bank_entries: tuple[MisconceptionEntry, ...]) -> tuple[tuple[str, str], ...]:
    """Every ``(code, mutant_symbolic)`` pair read off ``eq:``-prefixed
    trigger phrases across the bank. Non-``eq:`` trigger phrases (plain-text
    CBM triggers, ``bank_pattern``'s territory) are ignored here."""
    pairs: list[tuple[str, str]] = []
    for entry in bank_entries:
        for phrase in entry.trigger_phrases:
            if phrase.startswith(_EQ_MUTANT_PREFIX):
                mutant = phrase[len(_EQ_MUTANT_PREFIX) :].strip()
                if mutant:
                    pairs.append((entry.code, mutant))
    return tuple(pairs)


def _reference_equations(reference_graph: KGGraph) -> tuple[tuple[str, str], ...]:
    """Every ``(concept_key, symbolic)`` pair over the reference graph's
    equation nodes. ``concept_key`` is the reference node's ``node_id`` --
    ``centrality.py`` keys its ``{node_id: centrality}`` map the same way
    (section 5.1), so ``merge.py``'s ``centrality.get(concept_key, ...)``
    lookup only resolves if this module attaches findings by node_id, not by
    the (often-duplicated or absent) display label."""
    pairs: list[tuple[str, str]] = []
    for node in reference_graph.by_type("equation"):
        symbolic = student_surface_text(node)
        if not symbolic:
            continue
        pairs.append((node.node_id, symbolic))
    return tuple(pairs)


def detect_sign_veto(
    student_graph: KGGraph,
    reference_graph: KGGraph,
    *,
    bank_entries: tuple[MisconceptionEntry, ...] = (),
) -> tuple[ConceptFinding, ...]:
    """Deterministic Tier-1 equation sign-veto. See module docstring."""
    reference_equations = _reference_equations(reference_graph)
    mutants = _mutant_equations(bank_entries)
    if not reference_equations and not mutants:
        return ()

    findings: list[ConceptFinding] = []
    for student_node in student_graph.by_type("equation"):
        student_symbolic = student_surface_text(student_node)
        if not student_symbolic:
            continue

        # Correct: sign-exact match to any reference equation -> no finding.
        matched_reference = False
        for _concept_key, ref_symbolic in reference_equations:
            if _symbolic_equiv(student_symbolic, ref_symbolic, mappings={}):
                matched_reference = True
                break
        if matched_reference:
            continue

        # Otherwise: does it match a pre-authored sign mutant of a bank
        # equation? First mutant match wins (deterministic, first-authored).
        for code, mutant_symbolic in mutants:
            if not _symbolic_equiv(student_symbolic, mutant_symbolic, mappings={}):
                continue
            concept_key = _attach_concept_key(reference_equations, student_node.node_id)
            findings.append(
                ConceptFinding(
                    concept_key=concept_key,
                    verdict="misconception",
                    confidence=1.0,
                    severity=0.0,
                    evidence_span=student_symbolic,
                    signature=f"misc.{code}" if not code.startswith("misc.") else code,
                    source="sympy_veto",
                    corroborated=True,
                )
            )
            break

    return tuple(findings)


def _attach_concept_key(
    reference_equations: tuple[tuple[str, str], ...],
    student_node_id: str,
) -> str:
    """The concept_key a mutant-matched finding attaches to: the reference
    equation the mutant is a sign-flip OF (the first reference equation on
    record), falling back to the student node's own id when the reference
    graph carries no equation nodes at all (a bare mutant bank with no
    matching reference context -- still a valid, attributable finding)."""
    if reference_equations:
        return reference_equations[0][0]
    return student_node_id
