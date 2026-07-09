"""Prompt content regression tests — ensure critical rules are present in prompts."""
from __future__ import annotations

from ai.prompts.tutor import tutor_prompt
from ai.prompts.relevance_guard import relevance_guard_prompt


# ---------------------------------------------------------------------------
# Tutor prompt content assertions
# ---------------------------------------------------------------------------

def test_tutor_has_anti_redundancy_rule():
    prompt = tutor_prompt()
    # Rule intent: each point must add new info; no repeating the same fact.
    assert "Each point must add new information" in prompt
    assert "Do not repeat the same fact" in prompt


def test_tutor_has_type_specific_length_rules():
    prompt = tutor_prompt()
    assert "Yes/No" in prompt
    assert "2-4 sentences" in prompt
    # Should NOT have the old blanket-only word count rule (fixed: assertion now
    # matches the comment's intent — it was previously asserting presence).
    assert "match response length to question complexity" not in prompt


def test_tutor_has_partial_relevance_handling():
    prompt = tutor_prompt()
    assert "PARTIALLY RELEVANT" in prompt
    assert "not_relevant" in prompt


def test_tutor_has_short_question_handling():
    prompt = tutor_prompt()
    assert "SHORT / SIMPLE QUESTION HANDLING" in prompt
    assert "most likely interpretation" in prompt


def test_tutor_has_relevance_note_reference():
    prompt = tutor_prompt()
    assert "RelevanceNote" in prompt


def test_tutor_preserves_citation_requirement():
    prompt = tutor_prompt()
    assert "Every factual claim must have a citation" in prompt


def test_tutor_preserves_three_section_structure():
    prompt = tutor_prompt()
    assert "## Answer" in prompt
    assert "## Key Takeaway" in prompt
    assert "## Check Your Understanding" in prompt


# ---------------------------------------------------------------------------
# Relevance guard prompt content assertions
# ---------------------------------------------------------------------------

def test_relevance_guard_has_partial_option():
    prompt = relevance_guard_prompt("Fluid Mechanics")
    assert '"partial"' in prompt


def test_relevance_guard_has_on_topic_extraction():
    prompt = relevance_guard_prompt("Fluid Mechanics")
    assert "on_topic_portion" in prompt


def test_relevance_guard_has_three_values():
    prompt = relevance_guard_prompt("Test Subject")
    assert '"full"' in prompt
    assert '"partial"' in prompt
    assert '"none"' in prompt


def test_relevance_guard_includes_subject():
    prompt = relevance_guard_prompt("Thermodynamics")
    assert "Thermodynamics" in prompt


def test_relevance_guard_errs_toward_answering():
    prompt = relevance_guard_prompt("Physics")
    assert "err on the side of answering" in prompt
