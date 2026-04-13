"""Pydantic schema for a Concept Hierarchy DAG file.

A DAG file describes a topic cluster's concept graph: typed nodes
(concepts) and typed edges (requires | extends | excludes).
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Literal

import json
from pydantic import BaseModel, Field, field_validator


EdgeType = Literal["requires", "extends", "excludes"]


class DagNode(BaseModel):
    id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    prerequisites: List[str] = Field(default_factory=list)
    scope_boundary: List[str] = Field(default_factory=list)
    topic_cluster: str = Field(min_length=1)


class DagEdge(BaseModel):
    type: EdgeType
    from_: str = Field(alias="from", min_length=1)
    to: str = Field(min_length=1)

    model_config = {"populate_by_name": True}


class Dag(BaseModel):
    topic_cluster: str = Field(min_length=1)
    nodes: List[DagNode]
    edges: List[DagEdge]

    @field_validator("nodes")
    @classmethod
    def _unique_node_ids(cls, nodes: List[DagNode]) -> List[DagNode]:
        ids = [n.id for n in nodes]
        if len(ids) != len(set(ids)):
            dupes = {i for i in ids if ids.count(i) > 1}
            raise ValueError(f"duplicate node ids: {dupes}")
        return nodes

    def validate_edge_targets(self) -> None:
        node_ids = {n.id for n in self.nodes}
        for e in self.edges:
            if e.from_ not in node_ids:
                raise ValueError(f"edge.from refers to unknown node: {e.from_}")
            if e.to not in node_ids:
                raise ValueError(f"edge.to refers to unknown node: {e.to}")


def load_dag(path: str | Path) -> Dag:
    """Load and validate a DAG JSON file."""
    text = Path(path).read_text()
    dag = Dag.model_validate_json(text)
    dag.validate_edge_targets()
    return dag
