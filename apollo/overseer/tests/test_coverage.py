from apollo.overseer.coverage import compute_coverage


def _kg(equations=(), conditions=(), simplifications=()):
    return {
        "equation": [{"symbolic": s, "label": lab} for (s, lab) in equations],
        "definition": [],
        "condition": [{"label": lab, "applies_when": a} for (a, lab) in conditions],
        "simplification": [{"applies_when": a, "transformation": t} for (a, t) in simplifications],
        "variable_mapping": [],
    }


REFERENCE = [
    {"step": 1, "entry_type": "equation", "id": "continuity",
     "content": {"symbolic": "rho*A1*v1 - rho*A2*v2", "label": "Continuity"}, "depends_on": []},
    {"step": 2, "entry_type": "condition", "id": "incompressibility",
     "content": {"applies_when": "density is constant", "label": "Incompressibility"}, "depends_on": []},
    {"step": 3, "entry_type": "equation", "id": "bernoulli",
     "content": {"symbolic": "P1 + Rational(1,2)*rho*v1**2 + rho*g*h1 - (P2 + Rational(1,2)*rho*v2**2 + rho*g*h2)",
                 "label": "Bernoulli"}, "depends_on": ["incompressibility"]},
]


def test_all_covered():
    kg = _kg(
        equations=[
            ("rho*A1*v1 - rho*A2*v2", "Continuity"),
            ("P1 + Rational(1,2)*rho*v1**2 + rho*g*h1 - (P2 + Rational(1,2)*rho*v2**2 + rho*g*h2)", "Bernoulli"),
        ],
        conditions=[("density is constant", "Incompressibility")],
    )
    cov = compute_coverage(kg, REFERENCE)
    assert cov["continuity"] == "covered"
    assert cov["bernoulli"] == "covered"
    assert cov["incompressibility"] == "covered"


def test_missing_continuity():
    kg = _kg(
        equations=[
            ("P1 + Rational(1,2)*rho*v1**2 + rho*g*h1 - (P2 + Rational(1,2)*rho*v2**2 + rho*g*h2)", "Bernoulli"),
        ],
        conditions=[("density is constant", "Incompressibility")],
    )
    cov = compute_coverage(kg, REFERENCE)
    assert cov["continuity"] == "missing"
    assert cov["bernoulli"] == "covered"


def test_missing_condition():
    kg = _kg(equations=[("rho*A1*v1 - rho*A2*v2", "Continuity")])
    cov = compute_coverage(kg, REFERENCE)
    assert cov["incompressibility"] == "missing"


def test_empty_kg_all_missing():
    cov = compute_coverage(_kg(), REFERENCE)
    assert all(v == "missing" for v in cov.values())
