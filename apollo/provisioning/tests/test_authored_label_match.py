from types import SimpleNamespace

from apollo.provisioning.authored_sets.label_match import (
    build_solution_label_index,
    extract_problem_label,
    match_solution_label,
    normalize_label,
)


def test_normalize_label_variants():
    assert normalize_label("Problem 3") == "3"
    assert normalize_label("Q3") == "3"
    assert normalize_label("Question 12") == "12"
    assert normalize_label("3.") == "3"
    assert normalize_label("3)") == "3"
    assert normalize_label("Exercise 4(a)") == "4a"
    assert normalize_label("garbage") is None


def test_extract_problem_label_prefers_scraped_field():
    c = SimpleNamespace(label="Problem 7", problem_text="7. Find the moment ...")
    assert extract_problem_label(c) == "7"
    c2 = SimpleNamespace(label=None, problem_text="12. A cantilever ...")
    assert extract_problem_label(c2) == "12"
    c3 = SimpleNamespace(label=None, problem_text="A cantilever with no number")
    assert extract_problem_label(c3) is None


def test_index_and_match_single_block():
    chunks = [
        (10, "Solution 3\nWe begin by summing moments ...", 2),
        (11, "Problem 4 solution: integrate ...", 3),
    ]
    index = build_solution_label_index(chunks)
    hit = match_solution_label("3", index)
    assert hit is not None and hit[0][0] == 10


def test_normalize_label_ignores_separated_variable_letter():
    # A number followed by whitespace then a standalone variable token (common
    # in math solutions: "Solution 1  M = ...") must normalize to the number
    # only, never absorb the variable into a "1m"-style sub-label.
    assert normalize_label("Solution 1\nM = w*L^2/8") == "1"
    assert normalize_label("Problem 2 x = 3") == "2"
    # Genuinely adjacent / parenthesized sub-labels are still captured.
    assert normalize_label("Problem 4b") == "4b"
    assert normalize_label("Exercise 4 (a)") == "4a"


def test_index_not_polluted_by_following_variable():
    # The problem is labelled "1"; its solution block leads with a variable.
    # The index must key it as "1" so the paired problem actually matches.
    chunks = [(10, "Solution 1\nM = w*L^2/8 by summing moments.", 2)]
    index = build_solution_label_index(chunks)
    assert "1" in index
    hit = match_solution_label("1", index)
    assert hit is not None and hit[0][0] == 10


def test_index_includes_leading_number_labels():
    chunks = [(10, "1. (MC) Rivalry increases as competitors converge.", 2)]
    index = build_solution_label_index(chunks)
    assert match_solution_label("Problem 1", index) == chunks


def test_match_ambiguous_returns_none():
    chunks = [
        (10, "Solution 3 first copy ...", 1),
        (12, "Solution 3 duplicate appears again ...", 5),
    ]
    index = build_solution_label_index(chunks)
    assert match_solution_label("3", index) is None  # >=2 distinct blocks -> fall through
    assert match_solution_label("99", index) is None  # 0 matches -> fall through


def test_leading_number_index_keeps_ambiguity_fail_closed():
    chunks = [(10, "1. First answer", 1), (11, "1) Duplicate answer", 2)]
    assert match_solution_label("1", build_solution_label_index(chunks)) is None
