"""Pydantic schema for a variable normalization map.

Maps natural-language fluid-mechanics terms to canonical symbolic names
used by the SymPy solver.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict

from pydantic import BaseModel, Field


class VariableMap(BaseModel):
    topic_cluster: str = Field(min_length=1)
    mappings: Dict[str, str]


def load_variable_map(path: str | Path) -> VariableMap:
    """Load and validate a variable map JSON file."""
    text = Path(path).read_text()
    return VariableMap.model_validate_json(text)
