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


def test_match_ambiguous_returns_none():
    chunks = [
        (10, "Solution 3 first copy ...", 1),
        (12, "Solution 3 duplicate appears again ...", 5),
    ]
    index = build_solution_label_index(chunks)
    assert match_solution_label("3", index) is None  # >=2 distinct blocks -> fall through
    assert match_solution_label("99", index) is None  # 0 matches -> fall through
