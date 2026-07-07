"""Resolver V2 view-cache loader + ``RefNode`` builder (design §5.2, task T4).

The view cache is a single committed JSON generated OFFLINE by
``scripts/generate_resolver_v2_views.py`` (T2):

    {"<concept_id>/<problem_id>": {"<entity_key>": ["view 1", ...]}, "_meta": {...}}

Runtime contract (§5.2): a missing file / unparseable JSON / missing problem
key DEGRADES to ``{}`` — the caller falls back to label-only views. Each
distinct degradation is logged ONCE per process (never per attempt) and this
module NEVER raises on cache problems.

``build_ref_nodes`` emits one :class:`RefNode` per distinct canonical key on
the union of the reference graph's declared paths, key-sorted. ``views[0]`` is
ALWAYS the payload step's ``content.label`` (fallback: the canonical key
itself); cached views are appended dedup'd, order-preserving. Misconception
candidates are never RefNodes — V2 scores reference content only.

Pure module: stdlib + resolver_v2 types only (the ``ReferenceGraph`` import is
type-checking-only, so importing this never pulls the graph_compare stack).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from apollo.resolver_v2.types import RefNode

if TYPE_CHECKING:  # pragma: no cover - annotation-only import
    from apollo.graph_compare.canonical import ReferenceGraph

_LOG = logging.getLogger(__name__)

#: The committed, offline-generated view cache (T2 output).
VIEWS_CACHE_PATH: Path = Path(__file__).resolve().parent / "views" / "views_cache.json"

#: Node type recorded when a path key has no matching reference node (should
#: not happen — reference validation guarantees paths ⊆ nodes — but the loader
#: contract is "degrade, never raise").
_UNKNOWN_NODE_TYPE: str = "unknown"

# Process-lived set of already-logged degradation messages so a missing cache
# logs once, not once per attempt (mirrors done_grading's
# ``_log_nli_import_failure_once`` pattern).
_DEGRADE_LOGGED: set[str] = set()


def _log_degrade_once(message: str) -> None:
    if message not in _DEGRADE_LOGGED:
        _LOG.warning("resolver_v2_views_degraded %s", message)
        _DEGRADE_LOGGED.add(message)


def _clean_views(raw: object) -> tuple[str, ...]:
    """Boundary validation for one cache entry: keep non-empty strings only."""
    if not isinstance(raw, list):
        return ()
    return tuple(v.strip() for v in raw if isinstance(v, str) and v.strip())


def load_views(concept_id: str, problem_id: str) -> dict[str, tuple[str, ...]]:
    """Load the cached affirmative views for one problem.

    Returns ``{entity_key: (view, ...)}``; every degradation path (missing
    file, bad JSON, missing problem key, malformed entry) returns ``{}`` /
    drops the entry and logs once — it never raises (§5.2).
    """
    cache_key = f"{concept_id}/{problem_id}"
    try:
        raw_text = VIEWS_CACHE_PATH.read_text(encoding="utf-8")
    except OSError:
        _log_degrade_once(f"cache_unreadable path={VIEWS_CACHE_PATH}")
        return {}
    try:
        cache = json.loads(raw_text)
    except ValueError:
        _log_degrade_once(f"cache_unparseable path={VIEWS_CACHE_PATH}")
        return {}
    if not isinstance(cache, dict):
        _log_degrade_once(f"cache_not_a_dict path={VIEWS_CACHE_PATH}")
        return {}
    entry = cache.get(cache_key)
    if not isinstance(entry, dict):
        _log_degrade_once(f"problem_missing key={cache_key}")
        return {}
    views_by_key: dict[str, tuple[str, ...]] = {}
    for entity_key, raw_views in entry.items():
        if not isinstance(entity_key, str):
            continue
        views = _clean_views(raw_views)
        if views:
            views_by_key[entity_key] = views
        else:
            _log_degrade_once(f"entry_malformed key={cache_key} entity={entity_key}")
    return views_by_key


def _labels_from_payload(problem_payload: dict) -> dict[str, str]:
    """``{entity_key: content.label}`` from the payload's reference solution.

    ``ReferenceGraph``'s ``CanonicalNode`` carries NO label (design §11), so
    labels must come from ``problem_payload["reference_solution"]`` steps.
    Malformed steps are skipped — the caller falls back to the canonical key.
    """
    steps = problem_payload.get("reference_solution")
    if isinstance(steps, dict):  # tolerate a {"steps": [...]} wrapper shape
        steps = steps.get("steps")
    labels: dict[str, str] = {}
    if not isinstance(steps, list):
        return labels
    for step in steps:
        if not isinstance(step, dict):
            continue
        key = step.get("entity_key")
        content = step.get("content")
        label = content.get("label") if isinstance(content, dict) else None
        if isinstance(key, str) and isinstance(label, str) and label.strip():
            labels[key] = label.strip()
    return labels


def build_ref_nodes(
    reference_graph: ReferenceGraph,
    problem_payload: dict,
    views_by_key: Mapping[str, tuple[str, ...]],
) -> tuple[RefNode, ...]:
    """One :class:`RefNode` per distinct key on the union of declared paths.

    Key-sorted (deterministic). ``label`` = payload step ``content.label``
    (fallback: the canonical key); ``views`` = ``(label,)`` + cached views,
    deduplicated order-preserving so ``views[0] == label`` ALWAYS holds.
    ``node_type`` comes from the reference graph node (already an ontology
    NodeType string); an unmatched path key degrades to ``"unknown"``.
    """
    labels = _labels_from_payload(problem_payload)
    node_types = {node.canonical_key: str(node.node_type) for node in reference_graph.nodes}
    path_keys = sorted({key for path in reference_graph.paths for key in path.canonical_keys})
    ref_nodes: list[RefNode] = []
    for key in path_keys:
        label = labels.get(key, key)
        seen = {label}
        extra_views: list[str] = []
        for view in views_by_key.get(key, ()):
            if view not in seen:
                seen.add(view)
                extra_views.append(view)
        ref_nodes.append(
            RefNode(
                canonical_key=key,
                node_type=node_types.get(key, _UNKNOWN_NODE_TYPE),
                label=label,
                views=(label, *extra_views),
            )
        )
    return tuple(ref_nodes)
