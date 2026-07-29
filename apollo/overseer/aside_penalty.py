"""Flat credit cap for Hoot-assisted rubric nodes (INTERACTION5).

Pure module: no IO, no LLM, no DB. A single total function,
:func:`apply_aside_caps`, applied to a coverage verdict BEFORE it fans out to the
two credit consumers — the topic lane (``topic_score._credit_for_node``, the
served grade of record) and the axis rubric lane (``rubric.compute_rubric``). Its
whole job is to guarantee that a node a Hoot lookup aside pre-explained can never
grade as fully ``covered`` and never earns more than ``cap`` credit, in EITHER
lane, for this attempt (flat cap, no earn-back — a topic Hoot answered stays
capped even if the student later teaches it well).

## Why the cap edits BOTH ``procedure_scores`` and ``per_step``

The two lanes read the coverage dict differently, so a one-sided edit would let
the lanes disagree about the same node:

* ``_credit_for_node`` (topic lane): for a graded node it reads
  ``procedure_scores[node]`` as the node's continuous credit and reads
  ``per_step[node] == "covered"`` only to pick the STATUS
  (covered / partial / missing). Capping ``procedure_scores`` caps the credit;
  forcing ``per_step[node] = "missing"`` forbids the ``"covered"`` status, so a
  capped node lands ``(capped, "partial")`` when ``capped > 0`` and
  ``(capped, "missing")`` when ``capped == 0`` — exactly the spec's rule.
* ``compute_rubric`` (legacy axis lane): the procedure axis reads
  ``procedure_scores`` (so the cap flows straight in), while the binary
  justification/simplification axes count a node ONLY when
  ``per_step[node] == "covered"``. Those binary axes have no partial
  representation — the strongest a cap can do there is drop the node below
  covered — so forcing ``per_step = "missing"`` downgrades it consistently (it
  can never count as fully covered), satisfying "credit <= cap, never covered".

So for every assisted node we (a) write ``min(effective_credit, cap)`` into
``procedure_scores`` and (b) set ``per_step[node] = "missing"``. The node's
current EFFECTIVE credit is ``procedure_scores[node]`` when present, else ``1.0``
if it was ``"covered"`` (a binary covered node with no procedure score), else
``0.0`` — so a binary covered node lands at exactly ``cap`` / ``"partial"``
rather than being wrongly zeroed.

Unassisted nodes are carried through byte-for-byte. The input is never mutated: a
NEW top-level dict is returned with fresh copies of only the two maps that change
(``per_step``, ``procedure_scores``); every other key (``confidences``,
``negotiation_counts``, ``hoot_assisted``) is carried by reference and never
touched. An absent or all-False ``hoot_assisted`` map returns the input object
unchanged and an empty id tuple.
"""

from __future__ import annotations

import copy
import math

from apollo.overseer.coverage_contract import CoverageVerdict


def _effective_credit(node_id: str, per_step: dict, procedure_scores: dict) -> float:
    """The node's current pre-cap credit, mirroring ``_credit_for_node``.

    A graded node normally carries a ``procedure_scores`` entry (the continuous
    credit). If it only appears in ``per_step`` as ``"covered"`` — a binary
    covered node with no procedure score — its effective credit is the full
    ``1.0`` that ``_credit_for_node`` would award, so the cap lands it at ``cap``
    rather than zero. A malformed/NaN score coerces to ``0.0`` (clamped to
    ``[0, 1]``), matching the topic and rubric lanes' own ``_finite_score``."""
    if node_id in procedure_scores:
        try:
            value = float(procedure_scores[node_id])
        except (TypeError, ValueError):
            return 0.0
        if not math.isfinite(value):
            return 0.0
        return max(0.0, min(1.0, value))
    if per_step.get(node_id) == "covered":
        return 1.0
    return 0.0


def apply_aside_caps(
    coverage: CoverageVerdict, *, cap: float = 0.5
) -> tuple[CoverageVerdict, tuple[str, ...]]:
    """Cap every Hoot-assisted node's credit at ``cap`` and forbid ``covered``.

    Returns ``(new_coverage, assisted_node_ids)`` where ``assisted_node_ids`` is
    the sorted tuple of nodes that were capped. When ``coverage`` carries no
    ``hoot_assisted`` map, or no node in it is ``True``, the SAME ``coverage``
    object is returned unchanged with an empty tuple — the flag-off / no-aside
    path is byte-identical.

    The input coverage dict is never mutated (see module docstring)."""
    assist_map = coverage.get("hoot_assisted") or {}
    assisted_ids = tuple(sorted(node_id for node_id, flag in assist_map.items() if flag))
    if not assisted_ids:
        return coverage, ()

    per_step = dict(coverage.get("per_step", {}) or {})
    procedure_scores = dict(coverage.get("procedure_scores", {}) or {})
    for node_id in assisted_ids:
        capped = min(_effective_credit(node_id, per_step, procedure_scores), cap)
        procedure_scores[node_id] = capped
        per_step[node_id] = "missing"

    # ``copy.copy`` keeps the CoverageVerdict type and carries the untouched maps
    # (confidences / negotiation_counts / hoot_assisted) by reference; the two
    # maps we edited are swapped for the fresh copies built above, so the input's
    # inner maps are never mutated.
    new_coverage = copy.copy(coverage)
    new_coverage["per_step"] = per_step
    new_coverage["procedure_scores"] = procedure_scores
    return new_coverage, assisted_ids


__all__ = ["apply_aside_caps"]
