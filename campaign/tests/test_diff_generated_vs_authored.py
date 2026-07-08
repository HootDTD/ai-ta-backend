"""Pure-function tests for the authored calc-2 eval diff helpers."""

from __future__ import annotations

from campaign.scripts.diff_generated_vs_authored import (
    align_problems,
    diff_graph,
    score_concept_match,
    text_jaccard,
)

_CORPUS = [
    {
        "problem_id": "ibp-01",
        "problem_text": "Evaluate  integral x e^x dx.",
        "concept_slug": "integration-by-parts",
    },
    {
        "problem_id": "usub-01",
        "problem_text": "Evaluate  integral 2x cos(x^2) dx.",
        "concept_slug": "u-substitution",
    },
]


def _gen(text: str, slug: str | None, outcome: str = "promoted") -> dict:
    return {
        "payload": {"problem_text": text},
        "concept_slug": slug,
        "outcome": outcome,
    }


def test_text_jaccard_basics() -> None:
    assert text_jaccard("integral x e^x dx", "integral x e^x dx") == 1.0
    assert text_jaccard("", "anything") == 0.0


def test_align_matches_by_text_and_uses_each_corpus_entry_once() -> None:
    generated = [
        _gen("1. Evaluate integral x e^x dx.", "integration_by_parts"),
        _gen("2. Evaluate integral 2x cos(x^2) dx.", "u_substitution"),
        _gen("Totally unrelated statistics question about medians.", None),
    ]
    aligned = align_problems(generated, _CORPUS)
    assert aligned[0][1]["problem_id"] == "ibp-01"
    assert aligned[1][1]["problem_id"] == "usub-01"
    assert aligned[2][1] is None


def test_score_concept_match_normalizes_slugs_and_counts_misses() -> None:
    generated = [
        _gen("Evaluate integral x e^x dx.", "integration_by_parts"),
        _gen("Evaluate integral 2x cos(x^2) dx.", "partial-fractions"),
    ]
    report = score_concept_match(align_problems(generated, _CORPUS))
    assert report["total"] == 2 and report["correct"] == 1
    assert report["accuracy"] == 0.5
    assert report["misses"][0]["truth"] == "u_substitution"
    assert report["misses"][0]["predicted"] == "partial_fractions"


def test_score_concept_match_counts_no_match_holds() -> None:
    generated = [_gen("Evaluate integral x e^x dx.", None, outcome="held_for_review")]
    report = score_concept_match(align_problems(generated, _CORPUS))
    assert report["no_match_held"] == 1
    assert report["correct"] == 0 and report["total"] == 1


def test_diff_graph_reports_nodes_edges_and_opacity() -> None:
    generated = {
        "reference_solution": [
            {"id": "ibp_formula", "entry_type": "equation", "depends_on": []},
            {"id": "vm_a", "entry_type": "variable_mapping", "depends_on": ["ibp_formula"]},
        ]
    }
    committed = {
        "reference_solution": [
            {"id": "ibp_formula", "entry_type": "equation", "depends_on": []},
            {"id": "parts_assignment", "entry_type": "definition", "depends_on": []},
        ]
    }
    diff = diff_graph(generated, committed)
    assert diff["node_count"] == (2, 2)
    assert diff["shared_meaningful_ids"] == ["ibp_formula"]
    assert diff["generated_only"] == ["vm_a"]
    assert diff["committed_only"] == ["parts_assignment"]
    assert diff["edge_pairs_generated"] == [("vm_a", "ibp_formula")]
    assert diff["opaque_ids"] == ["vm_a"]
