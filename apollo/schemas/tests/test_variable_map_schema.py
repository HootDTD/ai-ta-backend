import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from apollo.schemas.variable_map import VariableMap, load_variable_map


def test_variable_map_accepts_valid():
    m = VariableMap.model_validate({
        "topic_cluster": "fluid_mechanics",
        "mappings": {"pressure": "P", "velocity": "v"},
    })
    assert m.mappings["pressure"] == "P"


def test_variable_map_rejects_empty_topic_cluster():
    with pytest.raises(ValidationError):
        VariableMap.model_validate({"topic_cluster": "", "mappings": {}})


def test_load_variable_map_reads_file(tmp_path: Path):
    p = tmp_path / "vm.json"
    p.write_text(json.dumps({"topic_cluster": "fm", "mappings": {"x": "X"}}))
    m = load_variable_map(p)
    assert m.mappings == {"x": "X"}
