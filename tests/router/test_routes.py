import pytest
from ai.router.routes import REGISTRY, RouteName, load_seed_utterances


@pytest.mark.unit
def test_registry_has_six_routes():
    assert {r.name for r in REGISTRY} == {
        "conceptual_explainer", "stepwise_problem_solver", "factual_lookup",
        "definition", "study_guide_generator", "clarify",
    }


@pytest.mark.unit
def test_seeds_provide_at_least_ten_utterances_per_route():
    seeds = load_seed_utterances()
    for r in REGISTRY:
        if r.name == "clarify":
            continue
        assert len(seeds[r.name]) >= 10, f"{r.name} has too few seed utterances"


@pytest.mark.unit
def test_route_name_is_string_enum():
    assert RouteName.CONCEPTUAL_EXPLAINER.value == "conceptual_explainer"
