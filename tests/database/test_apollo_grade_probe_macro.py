"""Pure-unit tests for the generalized scripts/apollo_grade_probe.py helpers.

The probe itself is integration-shaped (HTTP + Neo4j + Postgres); these tests
pin only its PURE logic — scenario lookup + generic fallback + partial backfill,
the per-problem intro transcript, variation parsing, the (problem, variation)
sweep plan, the parametric course-resolution SQL, and the difficulty mapping.
No DB, no network: the module imports ``neo4j``/``requests`` at top level (both
installed in the venv) but none of the tested functions touch them.
"""

from __future__ import annotations

import pytest

import scripts.apollo_grade_probe as p
from scripts._macro_scenarios import MACRO_PROBLEM_IDS


# --- scenario_messages: macro hit / generic fallback / partial backfill ------


def test_scenario_messages_macro_hit():
    msgs = p.scenario_messages("gdp_identity", "partial")
    assert msgs == p.MACRO_SCENARIOS["gdp_identity"]["partial"]


def test_scenario_messages_unknown_problem_uses_generic():
    assert p.scenario_messages("not_served", "weak") == p.GENERIC["weak"]


def test_scenario_messages_backfills_missing_variation_from_generic():
    # bernoulli scenario defines only strong/weak; partial backfills from GENERIC
    assert "partial" not in p.SCENARIOS["bernoulli_height_change_find_v2"]
    assert p.scenario_messages("bernoulli_height_change_find_v2", "partial") == p.GENERIC["partial"]


def test_scenario_messages_bernoulli_strong_preserved():
    assert p.scenario_messages("bernoulli_height_change_find_v2", "strong") == (
        p.SCENARIOS["bernoulli_height_change_find_v2"]["strong"]
    )


# --- intro_transcript --------------------------------------------------------


def test_intro_transcript_macro_problem():
    assert "expenditure approach" in p.intro_transcript("gdp_identity")


def test_intro_transcript_none_is_bernoulli():
    assert p.intro_transcript(None) == p.TRANSCRIPT


def test_intro_transcript_non_macro_is_bernoulli():
    assert p.intro_transcript("bernoulli_height_change_find_v2") == p.TRANSCRIPT


# --- parse_variations --------------------------------------------------------


def test_parse_variations_mode_both():
    assert p.parse_variations(None, mode="both") == ["strong", "weak"]


def test_parse_variations_single_mode():
    assert p.parse_variations(None, mode="partial") == ["partial"]


def test_parse_variations_explicit_list_wins():
    assert p.parse_variations("strong,partial,weak", mode="both") == ["strong", "partial", "weak"]


def test_parse_variations_trims_and_drops_blanks():
    assert p.parse_variations(" strong , , weak ") == ["strong", "weak"]


def test_parse_variations_rejects_unknown():
    with pytest.raises(SystemExit, match="unknown variation"):
        p.parse_variations("strong,brilliant")


# --- build_sweep -------------------------------------------------------------


def test_build_sweep_full_macro():
    sweep = p.build_sweep(variations=["strong", "partial", "weak"], macro=True, problem=None)
    assert len(sweep) == len(MACRO_PROBLEM_IDS) * 3
    assert sweep[0] == ("gdp_identity", "strong")
    assert sweep[-1] == ("real_gdp_growth", "weak")


def test_build_sweep_pinned_problem():
    assert p.build_sweep(variations=["strong", "weak"], macro=True, problem="nnp_chain") == [
        ("nnp_chain", "strong"),
        ("nnp_chain", "weak"),
    ]


def test_build_sweep_legacy_no_macro():
    # legacy: (None, variation) so the served problem decides the transcript
    assert p.build_sweep(variations=["strong", "weak"], macro=False, problem=None) == [
        (None, "strong"),
        (None, "weak"),
    ]


def test_build_sweep_problem_overrides_macro_flag():
    assert p.build_sweep(variations=["strong"], macro=False, problem="gdp_identity") == [
        ("gdp_identity", "strong"),
    ]


# --- _resolve_space_sql ------------------------------------------------------


def test_resolve_space_sql_both_slugs():
    sql, params = p._resolve_space_sql("gdp_components", "macroeconomics")
    assert "c.slug = :concept_slug" in sql
    assert "subj.slug = :subject_slug" in sql
    assert params == {"concept_slug": "gdp_components", "subject_slug": "macroeconomics"}


def test_resolve_space_sql_subject_only():
    sql, params = p._resolve_space_sql(None, "macroeconomics")
    assert "subj.slug = :subject_slug" in sql
    assert "c.slug" not in sql
    assert params == {"subject_slug": "macroeconomics"}


def test_resolve_space_sql_concept_only():
    sql, params = p._resolve_space_sql("gdp_components", None)
    assert params == {"concept_slug": "gdp_components"}
    assert "subj.slug" not in sql


def test_resolve_space_sql_defaults_to_bernoulli():
    sql, params = p._resolve_space_sql(None, None)
    assert params == {"concept_slug": "bernoulli_principle"}
    assert "ORDER BY subj.search_space_id LIMIT 1" in sql


# --- _difficulty_for ---------------------------------------------------------


@pytest.mark.parametrize("problem_id,expected", [
    ("gdp_identity", "intro"),
    ("net_exports_sign", "standard"),
    ("nnp_chain", "hard"),
    ("real_gdp_from_deflator", "standard"),
    ("real_gdp_growth", "hard"),
    (None, "intro"),
    ("unknown_problem", "intro"),
])
def test_difficulty_unique_per_problem(problem_id, expected):
    # Distinct difficulty per problem within a concept => from_hoot serves each
    # macro problem uniquely (no (concept, difficulty) collisions).
    assert p._difficulty_for(problem_id) == expected
