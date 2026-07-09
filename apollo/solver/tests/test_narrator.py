from apollo.solver.narrator import narrate_trace


def test_narrate_solved_trace_includes_value_and_substitution_step():
    trace = [
        {"op": "substitute_givens", "expr": "A1*v1 - A2*v2"},
        {"op": "substitute_givens", "expr": "P1 + 0.5*1000*v1**2 - P2"},
        {"op": "solve_system", "num_solutions": 1},
        {"op": "pick_real_solution", "target": "P2", "value": "194000"},
    ]
    text = narrate_trace(trace, status="solved", target="P2")
    assert "P2" in text
    assert "194000" in text


def test_narrate_stuck_trace_explains_missing_variables():
    trace = [
        {"op": "substitute_givens", "expr": "P1 + 0.5*rho*v1**2 - P2 - 0.5*rho*v2**2"},
        {"op": "solve_system", "num_solutions": 1},
        {"op": "parameterized_solution", "expression": "202000 - 500*v2**2"},
    ]
    text = narrate_trace(trace, status="stuck", target="P2", missing_variables=["v2"])
    assert "v2" in text
    assert "stuck" in text.lower() or "can't" in text.lower() or "couldn't" in text.lower() or "could not" in text.lower()


def test_narrate_empty_kg_stuck():
    trace = [{"op": "empty_kg"}]
    text = narrate_trace(trace, status="stuck", target="P2", missing_variables=["P2"])
    assert "nothing" in text.lower() or "empty" in text.lower() or "not taught" in text.lower()
