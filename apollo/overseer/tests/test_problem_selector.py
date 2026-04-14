import pytest

from apollo.errors import PoolExhaustedError
from apollo.overseer.problem_selector import list_problems_for_cluster, select_problem


def test_list_problems_for_fluid_mechanics_returns_authored_problems():
    problems = list_problems_for_cluster("fluid_mechanics")
    ids = [p.id for p in problems]
    assert "bernoulli_horizontal_pipe_find_p2" in ids
    assert len(problems) >= 5


def test_select_problem_intro_excludes_attempted():
    first = select_problem(
        cluster_id="fluid_mechanics",
        difficulty="intro",
        attempted_ids=[],
    )
    second = select_problem(
        cluster_id="fluid_mechanics",
        difficulty="intro",
        attempted_ids=[first.id],
    )
    assert second.id != first.id


def test_select_problem_raises_when_pool_exhausted():
    intro = list_problems_for_cluster("fluid_mechanics")
    intro_ids = [p.id for p in intro if p.difficulty == "intro"]

    with pytest.raises(PoolExhaustedError) as exc_info:
        select_problem(
            cluster_id="fluid_mechanics",
            difficulty="intro",
            attempted_ids=intro_ids,
        )
    assert exc_info.value.difficulty == "intro"
