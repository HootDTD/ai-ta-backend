"""Regression test for the S1 raw-graph harness scripts
(``campaign/out/f1c/run_s1_s2.py`` and ``campaign/out/f1/run_s1_s2.py``).

These are ad-hoc glue scripts (not `campaign/judges/` production code) but
their ``_fetch_subject_graph`` helper feeds the S1 judge directly, and it
used to hardcode ``edge_type="PRECEDES"`` for every ``apollo_entity_prereqs``
row. Per ``apollo/ontology/edges.py``, PRECEDES is legal ONLY between
``(procedure_step, procedure_step)`` pairs -- generic concept->concept
prerequisite links must be DEPENDS_ON (see
``.superpowers/sdd/a3-s1-adjudication.md`` sec 2A: this mislabel drove 26 of
57 S1 failures in the f1/f1c campaign runs). Loads each script as a module
(they are plain files, not packages) and drives ``_fetch_subject_graph``
against a fake asyncpg connection -- no real Postgres needed.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_module(rel_path: str, module_name: str) -> ModuleType:
    path = _REPO_ROOT / rel_path
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class _FakeConn:
    """Stands in for an asyncpg.Connection: routes .fetch() by table name
    referenced in the SQL text, returns plain dicts (asyncpg Records support
    the same __getitem__ access the harness uses)."""

    def __init__(self, *, nodes: list[dict[str, Any]], prereqs: list[dict[str, Any]]):
        self._nodes = nodes
        self._prereqs = prereqs

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        if "apollo_entity_prereqs" in query:
            return self._prereqs
        if "apollo_kg_entities" in query:
            return self._nodes
        if "apollo_concept_problems" in query:
            return []
        raise AssertionError(f"unexpected query: {query!r}")


def _run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize(
    "rel_path,module_name",
    [
        ("campaign/out/f1c/run_s1_s2.py", "_test_f1c_run_s1_s2"),
        ("campaign/out/f1/run_s1_s2.py", "_test_f1_run_s1_s2"),
    ],
)
def test_prereq_rows_emit_depends_on_not_precedes(rel_path: str, module_name: str):
    module = _load_module(rel_path, module_name)

    nodes = [
        {
            "id": 1,
            "canonical_key": "fluid_density",
            "kind": "concept",
            "display_name": "Fluid density",
            "payload": json.dumps({}),
        },
        {
            "id": 2,
            "canonical_key": "kinetic_energy_density",
            "kind": "concept",
            "display_name": "Kinetic energy density",
            "payload": json.dumps({}),
        },
    ]
    # from=dependent, to=prerequisite (kinetic_energy_density requires fluid_density)
    prereqs = [{"from_entity_id": 2, "to_entity_id": 1}]
    conn = _FakeConn(nodes=nodes, prereqs=prereqs)

    graph = _run(module._fetch_subject_graph(conn, "fluid_mechanics", [1]))

    assert graph["edges"] == [
        {
            "edge_type": "DEPENDS_ON",
            "from_node_id": "kinetic_energy_density",
            "to_node_id": "fluid_density",
        }
    ]
    assert "PRECEDES" not in {e["edge_type"] for e in graph["edges"]}
