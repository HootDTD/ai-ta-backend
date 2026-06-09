"""Diff-at-Done v1: parser prompt keeps typing, drops canonicalization.

Lives in its own file (test_parser.py is skipped legacy-V2) so the guard
actually runs: build_system_prompt is a pure template substitution that
needs no Neo4j / OpenAI wiring.
"""
from apollo.parser.prompt_builder import build_system_prompt
from apollo.subjects import load_concept


def test_parser_prompt_drops_canonical_pressure():
    concept = load_concept("fluid_mechanics", "bernoulli_principle")
    prompt = build_system_prompt(concept)
    low = prompt.lower()
    # Typing instructions remain.
    assert "procedure_step" in low
    assert "equation" in low
    # Canonicalization pressure removed.
    assert "canonical symbols for this concept" not in low
    assert "{{canonical_symbols_csv}}" not in prompt  # no unresolved slot
    # No other template slots left unresolved either.
    assert "{{subscript_convention}}" not in prompt
    assert "{{subscript_convention_short}}" not in prompt
    assert "{{concept_name}}" not in prompt
