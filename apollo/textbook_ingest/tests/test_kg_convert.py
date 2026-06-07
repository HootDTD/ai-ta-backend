from apollo.schemas.problem import ReferenceStep
from apollo.textbook_ingest.kg_convert import REFERENCE_ATTEMPT_ID, reference_steps_to_kg_graph


def test_converts_steps_to_kg_graph_with_depends_on_edges():
    steps = [
        ReferenceStep(step=1, entry_type="equation", id="continuity",
                      content={"symbolic": "rho*A1*v1 - rho*A2*v2", "label": "continuity",
                               "variables": ["rho", "A1", "v1", "A2", "v2"]}, depends_on=[]),
        ReferenceStep(step=2, entry_type="equation", id="bernoulli",
                      content={"symbolic": "P1 + 0.5*rho*v1**2 - P2 - 0.5*rho*v2**2",
                               "label": "bernoulli", "variables": ["P1", "rho", "v1", "P2", "v2"]},
                      depends_on=["continuity"]),
    ]
    g = reference_steps_to_kg_graph(steps)
    assert {n.node_id for n in g.nodes} == {"continuity", "bernoulli"}
    assert all(n.attempt_id == REFERENCE_ATTEMPT_ID for n in g.nodes)
    dep = [e for e in g.edges if e.edge_type.value == "DEPENDS_ON"]
    assert any(e.from_node_id == "bernoulli" and e.to_node_id == "continuity" for e in dep)
