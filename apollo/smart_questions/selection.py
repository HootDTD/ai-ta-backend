"""Deterministic question-target policy for Apollo's questioning loop.

Three rules live here, all enforced in code rather than in the prompt:

* **Graded-first (P1.2a).** Only ``GRADED_NODE_TYPES`` reference nodes are scored
  by the topic grader, so every open graded node must be probed before the ask
  budget is spent on ungraded (``definition`` / ``variable_mapping``) territory.
  Ordering puts graded nodes first and the reservation withholds the last
  ``len(open graded)`` questions from ungraded nodes.
* **Two asks per node (P2.4).** A node already probed ``MAX_ASKS_PER_NODE``
  times — or already ``understood`` — is not askable.
* **Contested first (P3.2 L2a).** A node the questioning engine labelled with a
  wrongness is *already* askable (it is not ``understood``); it is merely not
  prioritised. ``contested_ids`` sorts those to the front of the graded block so
  Apollo spends the signal while the student can still fix it. **The ask cap
  stays 2**: a contradiction consumes the second ask, it never creates a third.

The module is import-light on purpose (`ontology` + the grader's node-type set)
so `unified` and `controller` can both depend on it without a cycle; callers
pass tally rows structurally via the `NodeStateLike` / `NodeUpdateLike`
protocols instead of importing `unified`'s value objects here.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Protocol

from apollo.ontology import KGGraph, Node
from apollo.overseer.topic_score import _GRADED_NODE_TYPES

#: Node types the topic grader scores on — imported so there is exactly one
#: definition of "graded" in the codebase (`overseer/topic_score`).
GRADED_NODE_TYPES: frozenset[str] = frozenset(_GRADED_NODE_TYPES)

#: Hard per-node ask cap (was prompt-only; `times_asked` up to 3 was observed).
MAX_ASKS_PER_NODE = 2

_MASTERED_STATUS = "understood"


class NodeStateLike(Protocol):
    """The tally fields this policy reads (satisfied by `unified.TallyState`)."""

    @property
    def node_id(self) -> str: ...

    @property
    def status(self) -> str: ...

    @property
    def times_asked(self) -> int: ...


class NodeUpdateLike(Protocol):
    """This turn's tally change (satisfied by `unified.TallyUpdate`)."""

    @property
    def node_id(self) -> str: ...

    @property
    def status(self) -> str: ...


@dataclass(frozen=True)
class SelectionPolicy:
    """Which nodes Apollo may target this turn, and why."""

    graded_ids: tuple[str, ...]
    open_graded_ids: tuple[str, ...]
    askable_ids: tuple[str, ...]
    reserved_for_graded: int
    graded_only: bool

    @property
    def graded_topic_total(self) -> int:
        """Graded nodes in the reference graph (cross-repo `graded_topic_total`)."""
        return len(self.graded_ids)

    @property
    def open_graded_topics(self) -> int:
        """Graded nodes not yet `understood` (cross-repo `open_graded_topics`)."""
        return len(self.open_graded_ids)


def is_graded(node: Node) -> bool:
    return node.node_type in GRADED_NODE_TYPES


def ordered_nodes(reference_graph: KGGraph) -> list[Node]:
    """Reference nodes with graded types first, stable within each group."""
    graded = [node for node in reference_graph.nodes if is_graded(node)]
    ungraded = [node for node in reference_graph.nodes if not is_graded(node)]
    return [*graded, *ungraded]


def _contested_first(node_ids: list[str], contested_ids: Collection[str]) -> list[str]:
    """Stable partition: contested nodes first, every other node in place.

    An empty ``contested_ids`` returns the list unchanged — not a re-sorted copy
    that happens to be equal — so level 0/1 cannot differ from the pre-P3.2
    policy even if the partition itself were ever wrong.
    """
    if not contested_ids:
        return node_ids
    contested = set(contested_ids)
    return [
        *(node_id for node_id in node_ids if node_id in contested),
        *(node_id for node_id in node_ids if node_id not in contested),
    ]


def build_selection_policy(
    *,
    reference_graph: KGGraph,
    tally_state: Sequence[NodeStateLike] = (),
    updates: Sequence[NodeUpdateLike] = (),
    questions_asked: int = 0,
    cap: int,
    contested_ids: Collection[str] = (),
) -> SelectionPolicy:
    """Resolve the askable target set from the tally, this turn's updates, and budget.

    ``contested_ids`` (P3.2 L2a, active at wrongness level >= 2) only REORDERS
    the graded block. It never admits a node the ask cap or the ``understood``
    rule excluded, and it never changes ``reserved_for_graded`` or
    ``graded_only`` — a contradiction earns priority, never extra budget.
    """
    statuses = {item.node_id: item.status for item in tally_state}
    statuses.update({item.node_id: item.status for item in updates})
    asks = {item.node_id: item.times_asked for item in tally_state}

    graded_ids: list[str] = []
    open_graded: list[str] = []
    probeable_graded: list[str] = []
    probeable_ungraded: list[str] = []
    for node in ordered_nodes(reference_graph):
        node_id = node.node_id
        mastered = statuses.get(node_id, "missing") == _MASTERED_STATUS
        probeable = not mastered and asks.get(node_id, 0) < MAX_ASKS_PER_NODE
        if is_graded(node):
            graded_ids.append(node_id)
            if not mastered:
                open_graded.append(node_id)
            if probeable:
                probeable_graded.append(node_id)
        elif probeable:
            probeable_ungraded.append(node_id)

    probeable_graded = _contested_first(probeable_graded, contested_ids)
    reserved = len(probeable_graded)
    remaining = max(0, cap - questions_asked)
    graded_only = reserved > 0 and remaining <= reserved
    askable = tuple(probeable_graded) if graded_only else (*probeable_graded, *probeable_ungraded)
    return SelectionPolicy(
        graded_ids=tuple(graded_ids),
        open_graded_ids=tuple(open_graded),
        askable_ids=askable,
        reserved_for_graded=reserved,
        graded_only=graded_only,
    )


__all__ = [
    "GRADED_NODE_TYPES",
    "MAX_ASKS_PER_NODE",
    "NodeStateLike",
    "NodeUpdateLike",
    "SelectionPolicy",
    "build_selection_policy",
    "is_graded",
    "ordered_nodes",
]
