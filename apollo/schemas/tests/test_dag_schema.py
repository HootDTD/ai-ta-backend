import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from apollo.schemas.dag import Dag, load_dag


def _minimal_dag_dict():
    return {
        "topic_cluster": "fluid_mechanics",
        "nodes": [
            {"id": "a", "label": "A", "prerequisites": [], "scope_boundary": [], "topic_cluster": "fluid_mechanics"},
            {"id": "b", "label": "B", "prerequisites": ["a"], "scope_boundary": [], "topic_cluster": "fluid_mechanics"},
        ],
        "edges": [{"type": "requires", "from": "b", "to": "a"}],
    }


def test_dag_accepts_valid_minimal():
    dag = Dag.model_validate(_minimal_dag_dict())
    dag.validate_edge_targets()
    assert len(dag.nodes) == 2
    assert dag.edges[0].from_ == "b"


def test_dag_rejects_duplicate_node_ids():
    data = _minimal_dag_dict()
    data["nodes"].append(
        {"id": "a", "label": "A dup", "prerequisites": [], "scope_boundary": [], "topic_cluster": "fluid_mechanics"}
    )
    with pytest.raises(ValidationError, match="duplicate node ids"):
        Dag.model_validate(data)


def test_dag_rejects_edge_referring_to_unknown_node():
    data = _minimal_dag_dict()
    data["edges"].append({"type": "requires", "from": "b", "to": "nonexistent"})
    dag = Dag.model_validate(data)
    with pytest.raises(ValueError, match="unknown node: nonexistent"):
        dag.validate_edge_targets()


def test_dag_rejects_invalid_edge_type():
    data = _minimal_dag_dict()
    data["edges"][0]["type"] = "bogus"
    with pytest.raises(ValidationError):
        Dag.model_validate(data)


def test_load_dag_reads_file(tmp_path: Path):
    p = tmp_path / "dag.json"
    p.write_text(json.dumps(_minimal_dag_dict()))
    dag = load_dag(p)
    assert dag.topic_cluster == "fluid_mechanics"
