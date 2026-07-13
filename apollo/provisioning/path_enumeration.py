"""Fail-safe, subject-agnostic enumeration of alternative solution strategies.

V1 can only describe routes over steps already present in the minted reference
graph. Strategies that require additional steps are deliberately deferred. The
returned objects are a complete replacement for the legacy all-node path, so
they must jointly cover the graph and each must milestone a final-result sink.
"""

from __future__ import annotations

import json
import os
from typing import Any

from apollo.persistence.learner_model_seed import (
    normalize_declared_paths,
    validate_reference_graph,
)

_SYSTEM_PROMPT = """You identify genuinely different teach-back strategies.
Return [] unless the solution genuinely admits at least two distinct routes.
Qualitative and prose solutions almost always require []. Use only existing step
ids and never invent steps. The strategies must JOINTLY use every supplied step
id between them. Each strategy must list 1-3 indispensable milestones, including
at least one final-result step (a step no other step depends on). No strategy's
node set may equal or be a strict subset of another strategy's node set.
Strategies needing steps absent from this graph are out of scope for v1."""


def multi_path_enabled() -> bool:
    """Read the default-OFF rollout flag per call."""
    return os.getenv("APOLLO_MULTI_PATH", "").strip().lower() in {"1", "true", "yes", "on"}


def build_path_enumeration_schema() -> dict:
    """Return the strict closed schema for the single enumeration call."""
    return {
        "name": "apollo_strategy_paths",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "strategy_id": {"type": "string"},
                            "nodes": {"type": "array", "items": {"type": "string"}},
                            "milestones": {
                                "type": "array",
                                "items": {"type": "string"},
                                "minItems": 1,
                                "maxItems": 3,
                            },
                        },
                        "required": ["strategy_id", "nodes", "milestones"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["paths"],
            "additionalProperties": False,
        },
    }


def _step_summary(step: dict) -> dict:
    content = step.get("content") or {}
    summary = next(
        (
            content.get(field)
            for field in ("label", "action", "transformation", "applies_when", "symbolic")
            if content.get(field)
        ),
        "",
    )
    return {
        "id": step.get("id"),
        "entry_type": step.get("entry_type"),
        "depends_on": list(step.get("depends_on") or []),
        "summary": str(summary)[:500],
    }


def enumerate_strategy_paths(problem: dict, *, chat_fn) -> list[dict]:
    """Make one structured call and return only a valid replacement path set."""
    steps = problem.get("reference_solution") or []
    payload = {
        "problem_text": problem.get("problem_text", ""),
        "reference_solution": [_step_summary(step) for step in steps],
    }
    raw = chat_fn(
        purpose="path_enumeration",
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, sort_keys=True)},
        ],
        response_format={"type": "json_schema", "json_schema": build_path_enumeration_schema()},
        temperature=0.0,
    )
    decoded: Any = json.loads(raw)
    candidates = decoded.get("paths") if isinstance(decoded, dict) else None
    if not isinstance(candidates, list):
        raise ValueError("path enumeration response must contain a paths list")

    normalized_candidates: list[dict] = []
    for candidate in candidates:
        try:
            path = normalize_declared_paths([candidate])[0]
        except (IndexError, ValueError):
            return []
        normalized_candidates.append(
            {
                "strategy_id": path.strategy_id,
                "nodes": list(path.node_ids),
                "milestones": list(path.milestone_ids),
            }
        )
    if len(normalized_candidates) < 2:
        return []
    candidate_problem = {**problem, "declared_paths": normalized_candidates}
    if not validate_reference_graph(candidate_problem).ok:
        return []
    return normalized_candidates


__all__ = ["build_path_enumeration_schema", "enumerate_strategy_paths", "multi_path_enabled"]
